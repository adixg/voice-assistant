"""controller/gpu_scheduler/controller.py -- the routing logic is pure
enough to test without a live cluster: the kopf handlers just need plain
dicts (what kopf hands them for a Node's meta/status), and the one
kubernetes-client call is swapped for a recording fake."""
import controller.gpu_scheduler.controller as gs


class FakeLogger:
    def info(self, *a, **k):
        pass


class FakeAppsV1Api:
    def __init__(self):
        self.patches = []

    def patch_namespaced_deployment(self, name, namespace, body):
        self.patches.append((name, namespace, body))


class TestIsRtx4060:
    def test_true_for_the_labeled_node(self):
        assert gs._is_rtx4060({"labels": {"gpu-tier": "rtx4060"}}) is True

    def test_false_for_a_different_tier(self):
        assert gs._is_rtx4060({"labels": {"gpu-tier": "gtx1650"}}) is False

    def test_false_with_no_labels_at_all(self):
        assert gs._is_rtx4060({}) is False


class TestConditionsAreReady:
    def test_true_when_ready_condition_is_true(self):
        conditions = [{"type": "MemoryPressure", "status": "False"},
                      {"type": "Ready", "status": "True"}]
        assert gs._conditions_are_ready(conditions) is True

    def test_false_when_ready_condition_is_false(self):
        conditions = [{"type": "Ready", "status": "False"}]
        assert gs._conditions_are_ready(conditions) is False

    def test_false_with_no_conditions(self):
        assert gs._conditions_are_ready(None) is False
        assert gs._conditions_are_ready([]) is False


class TestOnNodeConditionChange:
    def test_ignores_a_node_that_is_not_the_rtx4060(self, monkeypatch):
        fake_apps = FakeAppsV1Api()
        monkeypatch.setattr(gs.client, "AppsV1Api", lambda: fake_apps)

        gs.on_node_condition_change(
            meta={"labels": {"gpu-tier": "gtx1650"}},
            status={"conditions": [{"type": "Ready", "status": "True"}]},
            logger=FakeLogger(),
        )

        assert fake_apps.patches == []

    def test_points_agent_at_the_4060_when_it_becomes_ready(self, monkeypatch):
        fake_apps = FakeAppsV1Api()
        monkeypatch.setattr(gs.client, "AppsV1Api", lambda: fake_apps)

        gs.on_node_condition_change(
            meta={"labels": {"gpu-tier": "rtx4060"}},
            status={"conditions": [{"type": "Ready", "status": "True"}]},
            logger=FakeLogger(),
        )

        assert len(fake_apps.patches) == 1
        name, namespace, body = fake_apps.patches[0]
        assert name == gs.AGENT_DEPLOYMENT
        assert namespace == gs.NAMESPACE
        env = body["spec"]["template"]["spec"]["containers"][0]["env"]
        assert env == [{"name": "OLLAMA_HOST", "value": gs.RTX4060_HOST}]

    def test_falls_back_to_the_1650_when_the_4060_goes_not_ready(self, monkeypatch):
        fake_apps = FakeAppsV1Api()
        monkeypatch.setattr(gs.client, "AppsV1Api", lambda: fake_apps)

        gs.on_node_condition_change(
            meta={"labels": {"gpu-tier": "rtx4060"}},
            status={"conditions": [{"type": "Ready", "status": "False"}]},
            logger=FakeLogger(),
        )

        env = fake_apps.patches[0][2]["spec"]["template"]["spec"]["containers"][0]["env"]
        assert env == [{"name": "OLLAMA_HOST", "value": gs.GTX1650_HOST}]


class TestOnNodeDelete:
    def test_ignores_a_deleted_node_that_is_not_the_rtx4060(self, monkeypatch):
        fake_apps = FakeAppsV1Api()
        monkeypatch.setattr(gs.client, "AppsV1Api", lambda: fake_apps)

        gs.on_node_delete(meta={"labels": {"gpu-tier": "gtx1650"}}, logger=FakeLogger())

        assert fake_apps.patches == []

    def test_falls_back_to_the_1650_when_the_4060_node_is_deleted(self, monkeypatch):
        fake_apps = FakeAppsV1Api()
        monkeypatch.setattr(gs.client, "AppsV1Api", lambda: fake_apps)

        gs.on_node_delete(meta={"labels": {"gpu-tier": "rtx4060"}}, logger=FakeLogger())

        env = fake_apps.patches[0][2]["spec"]["template"]["spec"]["containers"][0]["env"]
        assert env == [{"name": "OLLAMA_HOST", "value": gs.GTX1650_HOST}]
