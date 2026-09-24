"""controller/gpu_scheduler/controller.py -- the decisions are a pure function
of a State snapshot (decide), and the cluster reads/writes are swapped for
recording fakes, so none of this needs a live cluster."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import controller.gpu_scheduler.controller as gs


class FakeLogger:
    def info(self, *a, **k):
        pass


NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def state(**overrides):
    """The steady state with the 4060 serving and the standby still running."""
    base = dict(rtx_up=True, rtx_up_since=NOW - timedelta(seconds=gs.SCALE_DOWN_AFTER_SECONDS + 1),
                standby_replicas=1, standby_ready=True, keep_warm=False,
                agent_host=gs.RTX4060_HOST, agent_rolled_out=True)
    base.update(overrides)
    return gs.State(**base)


class TestDecide:
    def test_scales_the_standby_away_once_the_4060_has_been_up_long_enough(self):
        assert gs.decide(state(), NOW) == [("scale_standby", 0)]

    def test_debounce_keeps_the_standby_until_the_4060_has_been_up_a_while(self):
        just_up = state(rtx_up_since=NOW - timedelta(seconds=gs.SCALE_DOWN_AFTER_SECONDS - 1))
        assert gs.decide(just_up, NOW) == []

    def test_a_flapping_4060_never_scales_the_standby_down(self):
        # up for 10s, down, up for 10s ... each new "since" restarts the clock
        for age in (0, 10, 30, 59):
            assert gs.decide(state(rtx_up_since=NOW - timedelta(seconds=age)), NOW) == []

    def test_standby_stays_until_the_agent_has_finished_moving_to_the_4060(self):
        assert gs.decide(state(agent_rolled_out=False), NOW) == []

    def test_agent_is_moved_to_the_4060_before_the_standby_is_touched(self):
        assert gs.decide(state(agent_host=gs.GTX1650_HOST), NOW) == [("target", "rtx4060")]

    def test_nothing_to_do_when_the_standby_is_already_gone(self):
        assert gs.decide(state(standby_replicas=0, standby_ready=False), NOW) == []

    def test_keep_warm_pins_the_standby_running(self):
        assert gs.decide(state(keep_warm=True), NOW) == []

    def test_keep_warm_starts_a_standby_that_was_scaled_down(self):
        assert gs.decide(state(keep_warm=True, standby_replicas=0, standby_ready=False), NOW) == [
            ("scale_standby", 1)]

    def test_losing_the_4060_starts_the_standby_but_does_not_retarget_yet(self):
        lost = state(rtx_up=False, rtx_up_since=None, standby_replicas=0, standby_ready=False)
        assert gs.decide(lost, NOW) == [("scale_standby", 1)]

    def test_no_repeat_scale_up_while_the_standby_is_loading(self):
        loading = state(rtx_up=False, rtx_up_since=None, standby_replicas=1, standby_ready=False)
        assert gs.decide(loading, NOW) == []

    def test_agent_is_retargeted_to_the_standby_only_once_it_is_ready(self):
        lost = state(rtx_up=False, rtx_up_since=None, standby_replicas=1, standby_ready=True)
        assert gs.decide(lost, NOW) == [("target", "gtx1650")]

    def test_warm_standby_gives_an_immediate_failover(self):
        lost = state(rtx_up=False, rtx_up_since=None, keep_warm=True)
        assert gs.decide(lost, NOW) == [("target", "gtx1650")]

    def test_no_action_when_already_on_the_standby_and_the_4060_is_down(self):
        settled = state(rtx_up=False, rtx_up_since=None, agent_host=gs.GTX1650_HOST)
        assert gs.decide(settled, NOW) == []

    def test_unknown_agent_target_is_corrected(self):
        assert gs.decide(state(agent_host=None), NOW) == [("target", "rtx4060")]


class TestIsRtx4060:
    def test_true_for_the_labeled_node(self):
        assert gs._is_rtx4060({"labels": {"gpu-tier": "rtx4060"}}) is True

    def test_false_for_a_different_tier(self):
        assert gs._is_rtx4060({"labels": {"gpu-tier": "gtx1650"}}) is False

    def test_false_with_no_labels_at_all(self):
        assert gs._is_rtx4060({}) is False


def cond(kind, status, ago):
    return NS(type=kind, status=status, last_transition_time=NOW - timedelta(seconds=ago))


def deployment(replicas=1, ready=1, conditions=(), annotations=None, env=None, generation=1, observed=1,
               updated=None, unavailable=None):
    return NS(
        metadata=NS(annotations=annotations, generation=generation),
        spec=NS(replicas=replicas, template=NS(spec=NS(containers=[
            NS(name="agent", env=[NS(name=k, value=v) for k, v in (env or {}).items()])]))),
        status=NS(ready_replicas=ready, conditions=list(conditions), observed_generation=observed,
                  updated_replicas=replicas if updated is None else updated,
                  unavailable_replicas=unavailable),
    )


class FakeCluster:
    def __init__(self, nodes, deployments):
        self.nodes, self.deployments, self.patches = nodes, deployments, []

    def list_node(self, label_selector):
        assert label_selector == "gpu-tier=rtx4060"
        return NS(items=self.nodes)

    def read_namespaced_deployment(self, name, namespace):
        assert namespace == gs.NAMESPACE
        return self.deployments[name]

    def patch_namespaced_deployment(self, name, namespace, body):
        self.patches.append((name, body))


def install(monkeypatch, cluster):
    monkeypatch.setattr(gs.client, "CoreV1Api", lambda: cluster)
    monkeypatch.setattr(gs.client, "AppsV1Api", lambda: cluster)


def cluster_with(node_ready=True, rtx_ready=1, standby=None, agent=None, node_age=300, rtx_age=300):
    node = NS(status=NS(conditions=[cond("Ready", "True" if node_ready else "False", node_age)]))
    return FakeCluster([node], {
        gs.RTX4060_DEPLOYMENT: deployment(ready=rtx_ready, conditions=[cond("Available", "True", rtx_age)]),
        gs.STANDBY_DEPLOYMENT: standby or deployment(),
        gs.AGENT_DEPLOYMENT: agent or deployment(env={"LLM_HOST": gs.RTX4060_HOST}),
    })


class TestReadState:
    def test_healthy_4060_reports_up_since_the_later_of_node_and_server(self, monkeypatch):
        install(monkeypatch, cluster_with(node_age=300, rtx_age=20))
        st = gs.read_state()
        assert st.rtx_up and st.rtx_up_since == NOW - timedelta(seconds=20)
        assert st.agent_host == gs.RTX4060_HOST and st.agent_rolled_out and st.standby_ready

    def test_4060_node_ready_but_its_server_down_counts_as_down(self, monkeypatch):
        install(monkeypatch, cluster_with(rtx_ready=0))
        assert gs.read_state().rtx_up is False

    def test_4060_server_up_but_node_not_ready_counts_as_down(self, monkeypatch):
        install(monkeypatch, cluster_with(node_ready=False))
        assert gs.read_state().rtx_up is False

    def test_4060_node_missing_counts_as_down(self, monkeypatch):
        cluster = cluster_with()
        cluster.nodes = []
        install(monkeypatch, cluster)
        assert gs.read_state().rtx_up is False

    def test_keep_warm_annotation_is_read(self, monkeypatch):
        install(monkeypatch, cluster_with(standby=deployment(annotations={gs.KEEP_WARM_ANNOTATION: "True"})))
        assert gs.read_state().keep_warm is True

    def test_agent_mid_rollout_is_not_rolled_out(self, monkeypatch):
        install(monkeypatch, cluster_with(agent=deployment(env={"LLM_HOST": gs.RTX4060_HOST},
                                                           generation=2, observed=1)))
        assert gs.read_state().agent_rolled_out is False
        install(monkeypatch, cluster_with(agent=deployment(env={"LLM_HOST": gs.RTX4060_HOST}, unavailable=1)))
        assert gs.read_state().agent_rolled_out is False


class TestReconcile:
    def test_scales_the_standby_to_zero_when_the_4060_is_stable(self, monkeypatch):
        cluster = cluster_with()
        install(monkeypatch, cluster)
        monkeypatch.setattr(gs, "datetime", NS(now=lambda tz: NOW))

        gs.reconcile(FakeLogger())

        assert cluster.patches == [(gs.STANDBY_DEPLOYMENT, {"spec": {"replicas": 0}})]

    def test_loss_of_the_4060_scales_the_standby_up_and_leaves_agent_alone(self, monkeypatch):
        cluster = cluster_with(node_ready=False, standby=deployment(replicas=0, ready=0))
        install(monkeypatch, cluster)
        monkeypatch.setattr(gs, "datetime", NS(now=lambda tz: NOW))

        gs.reconcile(FakeLogger())

        assert cluster.patches == [(gs.STANDBY_DEPLOYMENT, {"spec": {"replicas": 1}})]

    def test_agent_is_pointed_at_the_standby_once_it_is_ready(self, monkeypatch):
        cluster = cluster_with(node_ready=False)
        install(monkeypatch, cluster)
        monkeypatch.setattr(gs, "datetime", NS(now=lambda tz: NOW))

        gs.reconcile(FakeLogger())

        name, body = cluster.patches[0]
        assert name == gs.AGENT_DEPLOYMENT
        env = body["spec"]["template"]["spec"]["containers"][0]["env"]
        assert env == [{"name": "LLM_HOST", "value": gs.GTX1650_HOST},
                       {"name": "LLM_MODEL", "value": gs.GTX1650_MODEL}]


class TestHandlers:
    def test_ignores_a_node_that_is_not_the_rtx4060(self, monkeypatch):
        calls = []
        monkeypatch.setattr(gs, "reconcile", lambda logger: calls.append(1))

        gs.on_node_condition_change(meta={"labels": {"gpu-tier": "gtx1650"}}, logger=FakeLogger())
        gs.on_node_delete(meta={"labels": {"gpu-tier": "gtx1650"}}, logger=FakeLogger())

        assert calls == []

    def test_4060_condition_change_and_deletion_reconcile(self, monkeypatch):
        calls = []
        monkeypatch.setattr(gs, "reconcile", lambda logger: calls.append(1))

        gs.on_node_condition_change(meta={"labels": {"gpu-tier": "rtx4060"}}, logger=FakeLogger())
        gs.on_node_delete(meta={"labels": {"gpu-tier": "rtx4060"}}, logger=FakeLogger())

        assert calls == [1, 1]

    def test_the_timer_reconciles(self, monkeypatch):
        calls = []
        monkeypatch.setattr(gs, "reconcile", lambda logger: calls.append(1))
        gs.periodic_reconcile(logger=FakeLogger())
        assert calls == [1]
