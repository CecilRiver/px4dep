"""Unit tests for uORB endpoint and publication/subscription edges."""

from __future__ import annotations

from px4dep.uorb_flow import build_uorb_flow, endpoint_id


TOPICS = [
    {"id": "topic-att", "name": "vehicle_attitude", "message_type": "vehicle_attitude_s"},
    {"id": "topic-status", "name": "estimator_status", "message_type": "estimator_status_s"},
]


def test_declaration_initializer_and_call_merge_into_one_endpoint():
    records = [
        {
            "record": "uorb_decl",
            "usr": "u1",
            "qualified_name": "M::_att_sub",
            "variable": "_att_sub",
            "role": "subscription",
            "uorb_class": "Subscription",
            "topics": ["vehicle_attitude"],
            "file": "src/m.hpp",
            "line": 10,
            "column": 2,
        },
        {
            "record": "uorb_init",
            "receiver_usr": "u1",
            "receiver": "_att_sub",
            "role": "subscription",
            "uorb_class": "Subscription",
            "topics": ["vehicle_attitude"],
            "topic_expr": "_att_sub{ORB_ID(vehicle_attitude)}",
            "file": "src/m.cpp",
            "line": 20,
            "column": 4,
        },
        {
            "record": "uorb_call",
            "receiver_usr": "u1",
            "receiver": "_att_sub",
            "receiver_type": "uORB::Subscription",
            "caller_usr": "fu",
            "caller": "M::update",
            "operation": "Update",
            "file": "src/m.cpp",
            "line": 30,
            "column": 4,
        },
    ]

    nodes, edges, metrics, unresolved = build_uorb_flow(
        None, records, TOPICS, [], {"fu": "fn1"}, "commit", "variant"
    )

    assert len(nodes) == 1
    endpoint = nodes[0]
    assert endpoint["id"] == endpoint_id("commit", "variant", "u1", "M::_att_sub")
    assert endpoint["declaration_status"] == "declared"
    assert endpoint["topic_expr"] == "_att_sub{ORB_ID(vehicle_attitude)}"
    assert unresolved == []

    assert sorted(e["relation"] for e in edges) == ["subscribes", "uses_uorb"]
    assert metrics["uorb_declarations"] == 1
    assert metrics["uorb_initializers"] == 1
    assert metrics["uorb_calls"] == 1
    assert metrics["endpoints_declared"] == 1
    assert metrics["endpoint_topic_edges"] == 1
    assert metrics["function_endpoint_edges"] == 1
    assert metrics["calls_by_operation"] == {"Update": 1}


def test_call_only_endpoint_without_topic_is_unresolved():
    records = [
        {
            "record": "uorb_call",
            "receiver_usr": "u2",
            "receiver": "pub",
            "receiver_type": "uORB::Publication<vehicle_attitude_s>",
            "caller_usr": "fu",
            "caller": "M::update",
            "operation": "publish",
            "file": "src/m.cpp",
            "line": 40,
            "column": 4,
        }
    ]

    nodes, edges, metrics, unresolved = build_uorb_flow(
        None, records, TOPICS, [], {"fu": "fn1"}, "commit", "variant"
    )

    assert len(nodes) == 1
    assert nodes[0]["declaration_status"] == "call_only"
    # The caller link is still evidence even though the topic is unknown.
    assert [e["relation"] for e in edges] == ["uses_uorb"]
    assert [u["kind"] for u in unresolved] == ["uorb_endpoint_without_topic"]
    assert metrics["endpoints_call_only"] == 1


def test_unknown_topic_is_unresolved():
    records = [
        {
            "record": "uorb_decl",
            "usr": "u3",
            "qualified_name": "M::_ghost_sub",
            "variable": "_ghost_sub",
            "role": "subscription",
            "topics": ["ghost_topic"],
            "file": "src/m.hpp",
            "line": 10,
            "column": 2,
        }
    ]
    _nodes, edges, metrics, unresolved = build_uorb_flow(
        None, records, TOPICS, [], {}, "commit", "variant"
    )
    assert edges == []
    assert [u["kind"] for u in unresolved] == ["uorb_topic_not_found"]
    assert metrics["endpoint_topic_edges"] == 0


def test_multi_topic_endpoint_uses_medium_confidence():
    records = [
        {
            "record": "uorb_init",
            "receiver_usr": "u4",
            "receiver": "_multi_pub",
            "role": "publication",
            "uorb_class": "PublicationMulti",
            "topics": ["vehicle_attitude", "estimator_status"],
            "topic_expr": "multi ? ORB_ID(estimator_status) : ORB_ID(vehicle_attitude)",
            "file": "src/m.cpp",
            "line": 50,
            "column": 2,
        }
    ]
    _nodes, edges, _metrics, unresolved = build_uorb_flow(
        None, records, TOPICS, [], {}, "commit", "variant"
    )
    assert unresolved == []
    assert [e["relation"] for e in edges] == ["publishes", "publishes"]
    assert {e["confidence"] for e in edges} == {"medium"}
