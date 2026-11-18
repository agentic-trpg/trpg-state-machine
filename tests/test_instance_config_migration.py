"""Container weights reflect candidate transfers and return to their original values on rollback."""

import pytest
from lock_support import CRED
from test_world_containers import BOX, container_data
from world_support import setup


def test_containment_reads_candidate_and_discards_rolled_back_weights(tmp_path, context):
    sm, sid, package, _ = setup(tmp_path, context, container_data())
    with sm:
        svc = sm._objects.containers
        assert svc.total_weight(BOX, svc._rows(sid), package) == 2
        with pytest.raises(RuntimeError), sm._transaction(write=True):
            sm._world_facts.transfer_item(sid, sm._world_facts.object(sid, CRED), container_id=BOX)
            candidate = svc._rows(sid)
            assert svc.total_weight(BOX, candidate, package) == 3
            raise RuntimeError("rollback")
        assert svc.total_weight(BOX, svc._rows(sid), package) == 2
        assert sm._world_facts.object(sid, CRED)["container_id"] is None
