# Copyright 1999-2019 Alibaba Group Holding Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy
import logging
from collections import defaultdict

from ..utils import SchedulerActor
from .core import OperandState

logger = logging.getLogger(__name__)


class BaseOperandActor(SchedulerActor):
    @staticmethod
    def gen_uid(session_id, op_key):
        return 's:operand$%s$%s' % (session_id, op_key)

    def __init__(self, session_id, graph_id, op_key, op_info, worker=None):
        super(BaseOperandActor, self).__init__()
        self._session_id = session_id
        self._graph_ids = [graph_id]
        self._info = copy.deepcopy(op_info)
        self._op_key = op_key
        self._op_path = '/sessions/%s/operands/%s' % (self._session_id, self._op_key)

        self._position = op_info.get('position')
        # worker actually assigned
        self._worker = worker

        self._op_name = op_info['op_name']
        self._state = self._last_state = op_info['state']
        io_meta = self._io_meta = op_info['io_meta']
        self._pred_keys = set(io_meta['predecessors'])
        self._succ_keys = set(io_meta['successors'])

        # set of finished predecessors, used to decide whether we should move the operand to ready
        self._finish_preds = set()
        # set of finished successors, used to detect whether we can do clean up
        self._finish_succs = set()

        # handlers of states. will be called when the state of the operand switches
        # from one to another
        self._state_handlers = {
            OperandState.UNSCHEDULED: self._on_unscheduled,
            OperandState.READY: self._on_ready,
            OperandState.RUNNING: self._on_running,
            OperandState.FINISHED: self._on_finished,
            OperandState.FREED: self._on_freed,
            OperandState.FATAL: self._on_fatal,
            OperandState.CANCELLING: self._on_cancelling,
            OperandState.CANCELLED: self._on_cancelled,
        }

        self._graph_refs = []
        self._cluster_info_ref = None
        self._assigner_ref = None
        self._resource_ref = None
        self._kv_store_ref = None
        self._chunk_meta_ref = None

    def post_create(self):
        from ..graph import GraphActor
        from ..assigner import AssignerActor
        from ..chunkmeta import ChunkMetaActor
        from ..kvstore import KVStoreActor
        from ..resource import ResourceActor

        self.set_cluster_info_ref()
        self._assigner_ref = self.ctx.actor_ref(AssignerActor.default_name())
        self._chunk_meta_ref = self.ctx.actor_ref(ChunkMetaActor.default_name())
        self._graph_refs.append(self.get_actor_ref(GraphActor.gen_name(self._session_id, self._graph_ids[0])))
        self._resource_ref = self.get_actor_ref(ResourceActor.default_name())

        self._kv_store_ref = self.ctx.actor_ref(KVStoreActor.default_name())
        if not self.ctx.has_actor(self._kv_store_ref):
            self._kv_store_ref = None

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, value):
        self._last_state = self._state
        if value != self._last_state:
            logger.debug('Operand %s(%s) state from %s to %s.', self._op_key, self._op_name,
                         self._last_state, value)
        self._state = value
        self._info['state'] = value.name
        futures = []
        for graph_ref in self._graph_refs:
            futures.append(graph_ref.set_operand_state(self._op_key, value.value, _tell=True, _wait=False))
        if self._kv_store_ref is not None:
            futures.append(self._kv_store_ref.write(
                '%s/state' % self._op_path, value.name, _tell=True, _wait=False))
        [f.result() for f in futures]

    @property
    def worker(self):
        return self._worker

    @worker.setter
    def worker(self, value):
        futures =[]
        for graph_ref in self._graph_refs:
            futures.append(graph_ref.set_operand_worker(self._op_key, value, _tell=True, _wait=False))
        if self._kv_store_ref is not None:
            if value:
                futures.append(self._kv_store_ref.write(
                    '%s/worker' % self._op_path, value, _tell=True, _wait=False))
            elif self._worker is not None:
                futures.append(self._kv_store_ref.delete(
                    '%s/worker' % self._op_path, silent=True, _tell=True, _wait=False))
        [f.result() for f in futures]
        self._worker = value

    def get_state(self):
        return self._state

    def _get_raw_execution_ref(self, uid=None, address=None):
        """
        Get raw ref of ExecutionActor on assigned worker. This method can be patched on debug
        """
        from ...worker import ExecutionActor
        uid = uid or ExecutionActor.default_name()

        return self.ctx.actor_ref(uid, address=address)

    def _get_operand_actor(self, key):
        """
        Get ref of OperandActor by operand key
        """
        op_uid = self.gen_uid(self._session_id, key)
        return self.ctx.actor_ref(op_uid, address=self.get_scheduler(op_uid))

    def _free_data_in_worker(self, data_keys, workers_list=None):
        """
        Free data on single worker
        :param data_keys: keys of data in chunk meta
        """
        from ...worker.chunkholder import ChunkHolderActor

        if not workers_list:
            workers_list = self._chunk_meta_ref.batch_get_workers(self._session_id, data_keys)
        worker_data = defaultdict(list)
        for data_key, endpoints in zip(data_keys, workers_list):
            if endpoints is None:
                continue
            for ep in endpoints:
                worker_data[ep].append(data_key)

        for ep, data_keys in worker_data.items():
            worker_cache_ref = self.ctx.actor_ref(ChunkHolderActor.default_name(), address=ep)
            worker_cache_ref.unregister_chunks(
                self._session_id, data_keys, _tell=True, _wait=False)
        self._chunk_meta_ref.batch_delete_meta(self._session_id, data_keys, _tell=True, _wait=False)

    def start_operand(self, state=None, **kwargs):
        """
        Start handling operand given self.state
        """
        if state:
            self.state = state

        kwargs = dict((k, v) for k, v in kwargs.items() if v is not None)
        io_meta = kwargs.pop('io_meta', None)
        if io_meta:
            self._io_meta.update(io_meta)
            self._info['io_meta'] = self._io_meta
        self._info.update(kwargs)

        self._state_handlers[self.state]()

    def stop_operand(self, state=OperandState.CANCELLING):
        """
        Stop operand by starting CANCELLING procedure
        """
        if self.state == OperandState.CANCELLING or self.state == OperandState.CANCELLED:
            return
        if self.state != state:
            self.start_operand(state)

    def add_running_predecessor(self, op_key, worker):
        pass

    def add_finished_predecessor(self, op_key, worker, output_sizes=None):
        self._finish_preds.add(op_key)

    def add_finished_successor(self, op_key, worker):
        self._finish_succs.add(op_key)

    def remove_finished_predecessor(self, op_key):
        try:
            self._finish_preds.remove(op_key)
        except KeyError:
            pass

    def remove_finished_successor(self, op_key):
        try:
            self._finish_succs.remove(op_key)
        except KeyError:
            pass

    def propose_descendant_workers(self, input_key, worker_scores, depth=1):
        pass

    def move_failover_state(self, from_states, state, new_target, dead_workers):
        raise NotImplementedError

    def _on_unscheduled(self):
        pass

    def _on_ready(self):
        pass

    def _on_running(self):
        pass

    def _on_finished(self):
        pass

    def _on_freed(self):
        pass

    def _on_fatal(self):
        pass

    def _on_cancelling(self):
        pass

    def _on_cancelled(self):
        pass
