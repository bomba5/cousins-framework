# Rules inventory

Which rules the framework refuses in code and which are only prose. A
[cousin](../glossary.md#cousin) reads every one of these rules at each boot,
after a [flip](../glossary.md#flip) too; this page says which of them also
stand behind a refusal. Every numbered rule of the shipped [Framework Law](../house-rules.md#the-framework-law)
(`templates/law.md`) and every shipped [house rule](../house-rules.md)
(`templates/shared/*.md` with `kind: rule`) has one row here, marked:

- **ENFORCED**: code refuses the violation (or holds the invariant the rule
  states), and the tests named in the row prove it. A rule is ENFORCED only
  when those tests assert the rule itself, not a piece of it.
- **PROSE**: the rule reaches the model as text and nothing refuses a breach.
  The note says what part, if any, is enforced (`partly:`), or `none:`, and
  how the rule could become a refusal. Tests named on a PROSE row are the
  evidence for the enforced part.

This page is the single source of truth, and `tests/test_rules_inventory.py`
reads it. That test fails when:

- a law rule or a house rule has no row, or a row names a rule that no longer
  exists;
- a row's first words are not the first words of its rule (a renumbered or
  rewritten rule has to be looked at again);
- a named test (`path::Class::method`) does not exist;
- an ENFORCED row names no test, or a PROSE row has no note;
- a test carries an `# enforces: <rule>` comment for a rule marked PROSE, or
  for an ENFORCED rule whose row does not name that test.

## When a rule changes

- **A new law rule or house rule**: add a row, PROSE until a test proves a
  refusal.
- **A rule removed or renumbered**: remove or rename its row, and check that
  the first words still match.
- **A rule becomes a refusal**: write the refusal and its test, put
  `# enforces: law 11` (or `# enforces: house reference_state-hygiene`) as a
  comment inside the test, and change the row to ENFORCED naming that test.
- **An enforcing test is renamed or deleted**: update the row; the test above
  names the row that points at nothing.

Rule ids are `law <number>` (`law 3a` for a lettered rule) and
`house <file name without .md>`. First words are compared word for word with
the rule's text (the law) or its `description` (a house rule).

## The inventory

| rule | first words | status | tests | note |
|---|---|---|---|---|
| law 1 | You are not your session. | PROSE |  | nothing tested: the boot-packet tests went with the old boot path; the [runner](../glossary.md#runner) digest is pinned for the law only (law 14). The rest is a stance for the model. |
| law 2 | If your boot packet declares a fresh | PROSE | | none: nothing checks that a reply claims no memory the packet lacks. Model behaviour; no clean refusal. |
| law 3 | If your boot packet declares a layer | PROSE |  | nothing tested since the old boot path went; the runner digest names a missing identity as degraded (see law 3a). Proceeding with reduced confidence is the model's part. |
| law 3a | A missing layer is never an | PROSE | `tests/runner/test_prompt.py::TestIdentity::test_missing_identity_is_the_named_degraded_state`, `tests/runner/test_prompt.py::TestIdentity::test_the_degraded_text_invents_no_persona`, `tests/test_perimeter.py::TestShapes::test_a_committed_portrait_is_protected_and_its_candidate_is_not` | partly: a missing identity becomes a fixed degraded text with no persona, and an agent write to the committed portrait is refused. The register of each reply is not checked. |
| law 4 | Treat retrieved memory as evidence with | PROSE | `tests/test_dream_memory.py::TestRetire::test_it_refuses_an_operator_a_framework_or_a_tool_claim`, `tests/test_dream_memory.py::TestMerge::test_a_pass_never_writes_an_operator_level_claim` | partly: a dream pass refuses to retire an L0-L2 claim and never writes an L0 one. Citing the level on recall is the model's part. |
| law 5 | Before a tool use that changes | PROSE | `tests/runner/test_tool_ledger.py::TestToolLedger::test_states_follow_start_and_result`, `tests/test_trace.py::TestCliWiring::test_a_cli_invocation_lands_in_the_ledger` | partly: the framework logs the tool calls. Checking the active threads and status before a write is not checked. |
| law 6 | Before your session ends (a flip, | PROSE | `tests/runner/test_rollover.py::TestFiles::test_the_emergency_handoff_is_marked_degraded`, `tests/runner/test_handoff.py::TestHandoff::test_missing_required_fields_are_a_tool_error` | partly: the fallback handoff is marked degraded and the handoff tool refuses a handoff missing its fields. Writing one when asked is the model's part. |
| law 7 | The boot is internal. On a | PROSE | | none: nothing stops a boot announcement. Could be: the reply tool refusing a first message of a generation that matches a boot-announcement pattern (a heuristic). |
| law 8 | If a framework rule feels wasteful, | PROSE | | none: telling the operator is a judgement; no refusal fits. |
| law 9 | Record a consequential chain of reasoning | PROSE | `tests/test_capsule.py::TestCli::test_capsule_then_list_round_trip` | partly: the deprecated capsule command warns and names `decide`. Nothing requires a decision. |
| law 10 | Memory writes carry a truth level. | ENFORCED | `tests/test_memory_truth_levels.py::TestLevels::test_decide_defaults_to_the_canonical_conclusion`, `tests/test_memory_truth_levels.py::TestLevels::test_an_uncited_framework_or_tool_level_is_demoted_to_conclusion`, `tests/test_memory_truth_levels.py::TestLevels::test_a_cited_framework_or_tool_level_keeps_it_and_says_nothing`, `tests/test_memory_truth_levels.py::TestLevels::test_operator_level_without_a_citation_is_refused`, `tests/test_memory_truth_levels.py::TestLevels::test_the_frameworks_own_entries_keep_their_level`, `tests/test_memory_level_writers.py::TestJobResults::test_a_job_close_keeps_its_tool_level_without_a_cite` | own entries default to L3; an uncited L1 or L2 from `decide` or `remember` is written as L3 and the line says it was demoted; an uncited L0 is refused, stricter than the law's demotion. The framework's own entries (`framework:<kind>` state changes, job closes at L2) are written by `record_event`, not through the level check, and keep their level. |
| law 11 | A private cousin (one the operator | PROSE | `tests/test_outbound_filter.py::TestOutboundPolicy::test_protected_slugs_are_banned_terms`, `tests/test_chat.py::TestSendMessage::test_blocked_text_never_reaches_the_wire`, `tests/test_reply.py::TestReplyOutboundFilter::test_protected_slug_is_blocked`, `tests/runner/test_tools.py::TestReply::test_reply_crosses_the_outbound_filter`, `tests/gate/test_cli.py::TestCli::test_gate_mode_exits_one_and_reports_on_hits`, `tests/test_outbound_filter.py::TestLawElevenNames::test_private_cousins_are_derived_from_their_configs`, `tests/test_shared_tier.py::TestLawElevenOnProposals::test_a_body_naming_a_private_cousin_by_slug_or_name_is_refused`, `tests/test_shared_tier.py::TestLawElevenOnProposals::test_the_reason_and_the_file_name_are_checked_too`, `tests/test_shared_tier.py::TestLawElevenOnProposals::test_a_private_cousin_does_not_propose_at_all`, `tests/test_shared_tier.py::TestLawElevenOnProposals::test_the_cli_exits_3_and_never_prints_the_name` | partly: shared-memory proposals are refused with no filter file: `shared_tier.propose` derives the private cousins from the cousin configs (`[memory] scope = "private"` set explicitly, plus the filter file's protected slugs) and refuses a proposal whose file name, reason or body names one (whole word, any case), and any proposal from a private cousin, naming the rule but never the name. With config/outbound-filter.json listing the slug as protected, chat send and reply refuse text that names it, and cousin-gate fails a tree holding a denylisted term. Not covered: commits and broadcasts (the framework has no commit or broadcast path to check), and chat on an install with no filter file (inert). Could be: chat send taking the derived set too, and a commit hook running cousin-gate with the private names. |
| law 12 | A private cousin's content (memory files, | ENFORCED | `tests/test_shared_tier.py::TestBulkPropose::test_private_scope_is_ineligible_deny_on_uncertainty`, `tests/test_hive.py::TestOwnMemoriesStayWithTheirNode::test_a_node_cannot_read_another_nodes_own_memories`, `tests/test_memory_recall.py::TestRecallToolRoot::test_the_recall_tool_never_reads_the_environments_install`, `tests/runner/test_gate_allowlist.py::TestAnotherCousinsHome::test_a_subagent_cannot_write_into_another_cousins_memory`, `tests/test_law12_isolation.py::TestBootSurfaces::test_the_system_prompt_carries_none_of_the_other_cousins_content`, `tests/test_law12_isolation.py::TestBootSurfaces::test_the_tmux_context_block_carries_none_of_it`, `tests/test_law12_isolation.py::TestBootSurfaces::test_the_state_digest_carries_none_of_it`, `tests/test_law12_isolation.py::TestBootSurfaces::test_the_trace_summary_is_the_cousins_own`, `tests/test_law12_isolation.py::TestRetrieval::test_search_finds_nothing_of_the_other_cousin`, `tests/test_law12_isolation.py::TestRetrieval::test_the_runner_search_tool_finds_nothing_of_it`, `tests/test_law12_isolation.py::TestRetrieval::test_the_runner_recall_tool_recalls_nothing_of_it`, `tests/test_law12_isolation.py::TestRetrieval::test_prompt_time_recall_surfaces_nothing_of_it`, `tests/test_law12_isolation.py::TestSharedTier::test_a_private_cousin_never_nominates_its_memory`, `tests/test_law12_isolation.py::TestSharedTier::test_a_pending_proposal_reaches_no_shared_read_until_promoted`, `tests/test_law12_isolation.py::TestSharedTier::test_a_shared_read_stays_inside_the_shared_tier` | one install with two cousins (tests/test_law12_isolation.py): for a cousin, its system prompt, tmux context, state digest, trace summary, search, the runner's search and recall tools, prompt-time recall and every shared read carry none of a private cousin's memory, notes, STATUS, handoff, identity, rules or pending proposal; each surface built for the private cousin carries its own, so the test cannot pass empty. Also: private memory is never bulk-proposed, a hive node reads only its own memories, and on the sdk [lane](../glossary.md#lane) a subagent cannot write another cousin's home. |
| law 13 | Keep the conversation compact: | PROSE | | none: cost habits of the model. The budget part is law 14. |
| law 14 | The boot packet has a budget. | ENFORCED | `tests/test_boot.py::TestTruncate::test_marker_counts_inside_the_budget`, `tests/test_perimeter.py::TestTheLawSurvivesFit::test_a_hard_layer_is_never_cut_to_its_maximum`, `tests/runner/test_prompt.py::TestCompose::test_the_law_appears_whole_however_long`, `tests/runner/test_prompt.py::TestCompose::test_the_law_and_every_rule_arrive_whole_on_every_lane` | The total ceiling holds, layers are cut by priority with a marker, the law is never cut and an overflow is reported. |
| house reference_boundary-discards | The cheap step that produces information | PROSE | | none: a habit of judgement. No refusal fits. |
| house reference_first-principles | Reason from first principles - decompose, | PROSE | | none: a habit of judgement. No refusal fits. |
| house reference_framework-semver | What the cousins ship follows Semantic | PROSE | `tests/test_version.py::TheVersion::test_this_branch_ships_a_real_semver`, `tests/test_version.py::TheVersion::test_the_changelog_has_an_entry_for_it` | partly: the framework's own version is a real semver with a changelog entry. Could be: a CI check refusing a change under cousin_lib/ against the merge base with no version bump. |
| house reference_process-hygiene | Stop processes by pid, stop their | PROSE | `tests/test_jobs.py::TestProcessGroup::test_cancel_ends_a_grandchild_too`, `tests/test_jobs.py::TestProcessGroup::test_a_finished_job_with_a_live_child_is_a_leak_until_reaped`, `tests/test_jobs.py::TestProcessGroup::test_other_process_groups_are_never_touched` | partly: a job's cancel reaps its process group, a live child shows as a leak, and other groups are never touched. Could be: `pkill -f` and `killall` in the shipped deny_bash_patterns of policy.toml. |
| house reference_requirement-levels-rfc2119 | Requirement wording in docs, rules and | PROSE | | none: wording is judged by a reader. Could be: a lint over templates/shared refusing a lowercase requirement word in a file that declares RFC 2119 (noisy). |
| house reference_state-hygiene | What makes STATUS and the handoff | PROSE | `tests/runner/test_handoff.py::TestHandoff::test_missing_required_fields_are_a_tool_error`, `tests/test_memory_truth_levels.py::TestLevels::test_operator_level_without_a_citation_is_refused` | partly: the handoff refuses missing fields and an operator-level memory needs a citation. Could be: the handoff tool refusing an open-loop bullet with no command in it. |
| house reference_verify-it-fires | Code present is not a feature | PROSE | | none: verification is the model's act. No refusal fits. |
