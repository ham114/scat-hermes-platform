from hermes_runtime.rag_operation import knowledge_run_scope


def test_native_principal_and_conversation_both_partition_run_ownership():
    original = knowledge_run_scope("service-a", "conversation-a")
    assert original == knowledge_run_scope("service-a", " conversation-a ")
    assert original != knowledge_run_scope("service-a", "conversation-b")
    assert original != knowledge_run_scope("service-b", "conversation-a")
    assert original != knowledge_run_scope("service-a", "")
    assert original != knowledge_run_scope("service-a\x00conversation-a", "")
