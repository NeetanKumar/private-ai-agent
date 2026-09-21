#!/usr/bin/env python3
"""Run every test suite and write docs/TEST_RESULTS.md from the real outcomes.

Rows are built from pytest results, never typed in. Each row lists the tests that back it, and a row
passes only if it matched at least one test and all of them passed. A row that matches nothing FAILS,
so renaming a test cannot silently drop a check. Checks that need a real GPU host are listed as
NOT RUN and are never counted as passing.
"""
from __future__ import annotations

import datetime
import platform
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "TEST_RESULTS.md"

# (check, how it is tested, [substrings of test ids that back it])
ROWS = [
    ("Phase 1: gateway", None, None),
    ("Gateway proxies a completion and reports the lane", "Stand-in model server over real sockets",
     ["test_completion_proxied_and_lane_reported", "test_on_demand_alias_selects_second_model", "test_unknown_model_rejected"]),
    ("Health endpoint reports model-server state", "Stand-in server up and down",
     ["test_health_ok", "test_health_degraded_when_model_down", "test_health_reports_degraded_when_model_is_down"]),
    ("Bearer token required, one session per user", "Requests with no token and a wrong token",
     ["test_requests_without_token_are_rejected", "test_bad_token_rejected"]),

    ("Phase 2: taint engine, lanes, audit log", None, None),
    ("Default ships `auto_route_clean: false`; both values are tested", "Read the shipped config; both settings run in the routing tests",
     ["test_shipped_default_is_auto_route_clean_false"]),
    ("Unregistered source is PRIVATE; CLEAN needs an explicit entry; context taint is the maximum; taint is sticky",
     "Unit tests on the registry, fragments and session",
     ["test_unregistered_source_is_private", "test_clean_requires_explicit_entry", "test_invalid_taint_value_rejected",
      "test_config_rejects_bad_source_taint", "test_context_taint_is_max_over_fragments", "test_propagation_rules",
      "test_session_taint_is_sticky", "test_model_output_inherits_context_taint"]),
    ("Canary strings in PRIVATE sources never appear in frontier requests across 200 mixed queries (run with both settings)",
     "200 seeded queries mixing clean, private and public sources; the stand-in frontier server records every body",
     ["test_canaries_in_private_sources_never_reach_the_frontier"]),
    ("Tainted session + explicit consent + `auto_route_clean=true` is still blocked", "Consent given, auto routing on, private context; explicit frontier request gets 403",
     ["test_tainted_session_with_consent_and_auto_true_is_still_blocked", "test_registered_private_source_taints_too"]),
    ("Turn N tainted, turn N+3 innocuous follow-up still blocked", "Four turns with consent and auto routing on",
     ["test_history_propagation_turn_n_taints_turn_n_plus_3", "test_a_documents_query_taints_the_session_for_later_turns"]),
    ("`auto_route_clean=false` + CLEAN context: no egress until consent", "Session scope and request scope; no connection before consent",
     ["test_clean_auto_false_answers_locally_and_offers_frontier", "test_clean_auto_false_egress_only_after_session_consent",
      "test_clean_auto_false_request_scope_needs_consent_every_request", "test_explicit_frontier_without_consent_is_refused"]),
    ("`auto_route_clean=true` + CLEAN context: egress occurs and an audit record is written", "Audit record fields and values checked",
     ["test_clean_auto_true_routes_to_frontier_and_audits"]),
    ("PRIVATE context: local only, no offer, no egress (both settings)", "Both `auto_route_clean` values with private context",
     ["test_private_auto_false_local_only_no_offer", "test_private_auto_true_local_only_no_offer"]),
    ("Audit entry count equals outbound connection count", "Real sockets: the stand-in frontier server counts TCP connections; blocked requests add none",
     ["test_audit_record_count_equals_outbound_connection_count"]),
    ("Killing the local model returns an error, never a frontier response", "Stop the stand-in model; check every path including streaming",
     ["test_killing_the_local_model_returns_an_error_not_a_frontier_response", "test_streaming_also_fails_closed", "test_fails_closed_when_model_down"]),
    ("Planted prompt-injection text claiming \"not sensitive\" does not alter taint", "Hostile context; client-supplied taint fields and reserved sources",
     ["test_prompt_injection_doc_does_not_alter_taint", "test_fragment_text_cannot_change_taint",
      "test_client_supplied_taint_field_is_ignored", "test_client_cannot_claim_a_reserved_source"]),
    ("`/new` is the only way taint is cleared", "Topic-change wording, case variants and partial matches must not clear it",
     ["test_new_command_clears_taint_and_only_it", "test_new_endpoint_also_clears_and_resets_consent"]),
    ("Frontier receives only the current user message; attachments rejected", "Inspect the recorded frontier request bodies",
     ["test_frontier_request_contains_only_the_current_user_turn", "test_attachments_are_blocked_from_the_frontier"]),
    ("Only the egress function can reach the frontier client", "Static check of imports",
     ["test_only_egress_imports_the_frontier_client"]),
    ("Audit log holds no prompt bodies or user data", "Search the log file for the prompt text; compare the hash",
     ["test_audit_log_holds_no_prompt_bodies_or_user_data"]),
    ("Frontier failure is an error, is still audited, and does not fall back", "Stop the stand-in frontier; remove the API key",
     ["test_frontier_failure_is_an_error_and_is_still_audited", "test_frontier_not_configured_makes_no_connection"]),
    ("Users are isolated; adding a user is a config change", "Second user added through config only",
     ["test_users_are_isolated_and_added_by_config_only"]),
    ("Explicit private lane and lane labelling", "Lane and taint fields on every response",
     ["test_explicit_private_lane_overrides_auto_routing", "test_private_stream_reports_lane", "test_frontier_stream_is_delivered_as_sse_with_lane"]),

    ("Phase 3: private documents", None, None),
    ("Golden set: recall@5 and MRR (offline stand-in embedder)", "50 questions over an 11-document corpus including a PDF",
     ["test_recall_mrr_and_gate_on_golden_set", "test_golden_set_shape", "test_golden_answers_exist_in_the_corpus"]),
    ("Off-topic no-answer questions get \"not in documents\" from the floor gate without calling a model", "10 no-answer questions; the 6 off-topic ones must be gated; the 4 near-miss ones are covered separately below",
     ["test_no_answer_gate_returns_empty_so_no_model_is_called", "test_no_hits_answers_not_in_documents_without_calling_any_model"]),
    ("User A can never retrieve user B's chunks", "Two users, hostile filters, whole golden corpus, and through the gateway",
     ["test_user_a_can_never_retrieve_user_b_chunks", "test_isolation_holds_across_the_shipped_golden_corpus", "test_gateway_isolates_users_documents"]),
    ("Chunks inherit source taint; documents use the private lane only", "Registered and unregistered sources; auto routing and consent on",
     ["test_chunks_inherit_source_taint", "test_documents_never_reach_the_frontier", "test_retrieved_chunk_taint_comes_from_its_document",
      "test_documents_answer_uses_retrieved_chunks_on_the_private_lane"]),
    ("Ingest md, txt and pdf; chunking; storage backend guard", "Sample files; broken and unsupported files",
     ["test_loads_md_txt_and_pdf", "test_rejects_unsupported_and_broken_files", "test_chunks_keep_heading_path", "test_chunk_overlap_carries_a_sentence",
      "test_reingesting_a_document_replaces_its_chunks", "test_store_refuses_to_mix_embedders"]),
    ("Retrieval fails closed; documents can be disabled", "Embedder down; feature switched off",
     ["test_retrieval_failure_fails_closed", "test_rag_can_be_disabled_by_config", "test_invalid_documents_flag_rejected"]),
    ("Instructions inside a document are data, not commands", "Document with a fake closing tag and instructions",
     ["test_instructions_inside_a_document_are_data_not_commands", "test_documents_stream_reports_citations"]),
    ("Evaluation harness computes faithfulness and correctness", "Stand-in answerer and judge (plumbing only)",
     ["test_generation_metrics_plumbing_with_stub_model"]),

    ("Phase 4: agent and permission layer", None, None),
    ("Every write, exec and web tool call is blocked at the gateway", "A misbehaving stand-in model issues 14 kinds of forbidden call; the execution endpoint is probed too",
     ["test_any_call_outside_the_readonly_allowlist_is_stripped", "test_execution_endpoint_refuses_anything_outside_the_allowlist",
      "test_config_cannot_enable_a_write_tool", "test_every_write_attempt_is_blocked_and_counted",
      "test_write_and_exec_are_refused_through_the_bridge_and_logged", "test_mixed_turn_keeps_only_the_permitted_call",
      "test_tool_not_offered_this_turn_is_blocked", "test_text_form_tool_calls_are_removed"]),
    ("Tool definitions come from the gateway; the model never sees the client's", "Client sends hostile definitions",
     ["test_model_only_sees_canonical_readonly_definitions", "test_no_tools_are_sent_when_none_are_permitted",
      "test_definitions_endpoint_lists_only_readonly_tools", "test_invalid_tools_field_rejected"]),
    ("Tool arguments validated; call count capped; ids issued by the gateway", "Malformed, oversized and extra arguments",
     ["test_invalid_arguments_are_blocked", "test_call_count_is_capped", "test_gateway_issues_ids", "test_tool_arguments_are_validated_at_execution_too"]),
    ("Path sandbox: no `..`, absolute paths, symlink escapes or other users' files", "Eight escape attempts, also through the tool bridge",
     ["test_path_sandbox_blocks_escapes_and_other_users", "test_users_only_see_their_own_files",
      "test_traversal_and_other_users_files_are_refused_through_the_bridge", "test_read_search_and_list_work_inside_the_users_folder"]),
    ("Tool results are Fragments with taint; tools cannot launder private data into CLEAN", "Default PRIVATE, argument taint, client-supplied labels",
     ["test_tool_output_defaults_to_private_and_taints_the_session", "test_argument_taint_flows_into_the_result",
      "test_client_cannot_declare_a_tool_result_clean", "test_clean_registered_tool_in_a_clean_session"]),
    ("Tool results are accepted only for calls the gateway issued", "Forged, replayed, blocked and cross-user results",
     ["test_results_are_only_accepted_for_calls_the_gateway_issued", "test_a_blocked_call_never_becomes_a_pending_call",
      "test_tool_results_are_isolated_per_user", "test_a_new_user_turn_closes_unanswered_calls", "test_documents_and_tool_results_cannot_be_combined"]),
    ("Tool output cannot break its wrapper or run `/new`", "Fake closing tag and slash command inside tool output",
     ["test_tool_output_cannot_break_out_of_its_wrapper_or_run_slash_commands", "test_full_round_trip_read_only_agent_flow"]),
    ("Injection corpus: 12 hostile documents x 4 channels cannot change lane, taint or exfiltrate", "A stand-in model that obeys every injection; assert lane, taint, zero frontier connections, blocked tools, sanitised reply",
     ["test_injection_cannot_change_lane_taint_or_exfiltrate"]),
    ("Injection corpus, streamed replies", "Same documents, replies streamed in 3-character pieces",
     ["test_injected_reply_is_sanitised_when_streamed_too", "test_corpus_has_twelve_documents", "test_a_clean_session_is_also_protected"]),
    ("Streaming cannot bypass the tool check", "Streamed reply with forbidden calls",
     ["test_streaming_with_tools_is_buffered_and_checked", "test_streaming_without_tools_drops_any_tool_call_deltas"]),
    ("20-task tool-use scorer works (stand-in models only)", "Correct, wrong-tool, never-stops and write-happy stand-ins; tasks solvable from real tool output",
     ["test_there_are_twenty_well_formed_tasks", "test_tasks_are_solvable_from_the_real_tool_outputs", "test_a_correct_model_scores_twenty_of_twenty",
      "test_a_model_that_picks_the_wrong_tool", "test_a_model_that_never_stops", "test_argument_matching_rules"]),
    ("Agent tool bridge and agent configuration", "Bridge run against a live gateway over stdio; config checks",
     ["test_agent_bridge"]),

    ("Built-in test UI (`/ui`)", None, None),
    ("UI is served with a strict content policy and only its own two assets", "Header checks, path-traversal probes, config switch",
     ["test_ui_is_served_with_a_strict_content_security_policy", "test_static_assets_are_served_and_nothing_else", "test_ui_can_be_switched_off_by_config"]),
    ("UI makes no outside requests and never renders server text as HTML", "Static scan of the page, script and stylesheet; script parses; every element id it uses exists",
     ["test_ui_source_makes_no_outside_requests", "test_ui_javascript_parses", "test_ui_ids_used_by_the_script_exist", "test_hidden_attribute_always_wins"]),
    ("Audit and security-log endpoints return only the caller's own records, with no prompt text", "Two users; limit and ordering; auth required",
     ["test_audit_endpoint_returns_only_the_callers_records", "test_audit_endpoint_orders_newest_first", "test_security_endpoint_returns_only_the_callers_events", "test_log_endpoints_require_auth"]),
    ("UI works in a real browser: login, chat, consent (both scopes), taint lock, /new, tools, logs, mobile layout", "Headless Chrome drives the real page against the real gateway with stand-in models; also fails on any content-policy violation",
     ["test_ui_session_scope_end_to_end", "test_ui_request_scope_end_to_end"]),

    ("Phase 5: packaging and hygiene", None, None),
    ("Secrets are never tracked; env example holds no values", "Scan tracked files", ["test_no_env_file_is_tracked", "test_env_example_holds_no_secret_values", "test_no_secret_shaped_strings"]),
    ("README and docs carry no personal references", "Scan for emails, home paths and account URLs; optional name scrub", ["test_docs_and_readme_carry_no", "test_readme_names_the_project", "test_no_tracked_file_contains_an_email"]),
    ("Nothing is published publicly by default", "Read the Compose file: loopback bind, no model port, internal network, locked-down container",
     ["test_gateway_port_defaults_to_loopback", "test_model_server_publishes_no_port", "test_gateway_container_is_locked_down", "test_egress_script_allows_only_https"]),
    ("README states the routing table and rules verbatim", "Exact-text check", ["test_readme_has_the_routing_table_verbatim", "test_required_docs_and_make_targets_exist"]),
]

PENDING = [
    ("Outside port scan shows nothing open", "`tests/test_portscan.sh` from another machine", "RUNBOOK 6.2"),
    ("Audit entries equal real outbound connections on the wire", "`tests/privacy/tcpdump_check.sh` on the host", "RUNBOOK 6.3"),
    ("Host firewall limits the gateway to the frontier API host", "`infra/egress-allowlist.sh`; verified by the capture above", "RUNBOOK 5"),
    ("A real local model answers through the gateway", "`curl` through the gateway on the GPU host", "RUNBOOK 6.1"),
    ("Killing the real Ollama container returns an error, not a frontier response", "Stop the container and send a request", "RUNBOOK 6.5"),
    ("Retrieval quality with the real embedding model, and `rag.min_score` calibration", "`tests/rag/report.py` floor sweep", "RUNBOOK 6.6"),
    ("Faithfulness and answer correctness on the golden set", "`tests/rag/report.py --live`", "RUNBOOK 6.6"),
    ("Tool-use score of the real daily-driver model on the 20-task set", "`tests/agent/run_eval.py --url ... --model ...`", "RUNBOOK 6.6"),
    ("A live frontier call, including the audit record and token counts", "Real API key on the host", "RUNBOOK 4"),
    ("OpenClaw runs with `agent/openclaw.json` and the tool bridge", "Install OpenClaw, validate config, run a task", "agent/README.md"),
]


def run(cmd, **kw):
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, **kw)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        junit = Path(tmp) / "junit.xml"
        print("running the full test suite (about three minutes)...", flush=True)
        p = run([sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider", f"--junitxml={junit}"])
        print(p.stdout.splitlines()[-1] if p.stdout.strip() else p.stderr[-400:])
        cases = []
        for tc in ET.parse(junit).getroot().iter("testcase"):
            failed = tc.find("failure") is not None or tc.find("error") is not None
            skipped = tc.find("skipped") is not None
            cases.append((f'{tc.get("classname")}.{tc.get("name")}', "fail" if failed else "skip" if skipped else "pass"))

    rag = run([sys.executable, "tests/rag/report.py"]).stdout
    tool = run([sys.executable, "tests/agent/run_eval.py", "--self-check"]).stdout

    def metric(pattern):
        m = re.search(pattern, rag)
        return m.group(1) if m else "n/a"
    recall, mrr = metric(r"recall@5\s*:\s*([\d.]+)"), metric(r"MRR\s*:\s*([\d.]+)")
    gated, falsea = metric(r"no-answer gated\s*:\s*(\d+/\d+)"), metric(r"false abstentions\s*:\s*(\d+/\d+)")

    used, table, failures = set(), [], 0
    for name, method, subs in ROWS:
        if method is None:
            table.append(f"| **{name}** | | | |")
            continue
        matched = [(i, s) for i, s in cases if any(x in i for x in subs)]
        used.update(i for i, _ in matched)
        n_pass = sum(s == "pass" for _, s in matched)
        n_fail = sum(s == "fail" for _, s in matched)
        skipped_only = bool(matched) and n_pass == 0 and n_fail == 0 and n_skip > 0
        ok = bool(matched) and n_fail == 0 and n_pass > 0
        n_skip = sum(s == "skip" for _, s in matched)
        result = (f"{n_pass}/{len(matched)} tests passed" + (f", {n_skip} skipped" if n_skip else "")) if matched else "no matching tests"
        if name.startswith("Golden set"):
            result += f"; recall@5 {recall}, MRR {mrr}"
        if name.startswith("Off-topic"):
            result += f"; floor gate closed on {gated} of all 10 (see note 2); false abstentions {falsea}"
        if name.startswith("20-task"):
            result += "; stand-in scored 20/20 (proves the harness only)"
        failures += not (ok or skipped_only)
        status = "PASS" if ok else ("SKIPPED (tool missing on this machine, not counted as passing)" if skipped_only else "FAIL")
        table.append(f"| {name} | {method} | {result} | {status} |")
    other = [(i, s) for i, s in cases if i not in used]
    o_pass, o_fail = sum(s == "pass" for _, s in other), sum(s == "fail" for _, s in other)
    failures += o_fail
    o_skip = len(other) - o_pass - o_fail
    other_result = f"{o_pass} passed" + (f", {o_skip} skipped (optional name scrub)" if o_skip else "")
    table.append(f"| Other tests (parametrised cases and helpers not listed above) | Same suites | {other_result} | "
                 f"{'PASS' if o_fail == 0 else 'FAIL'} |")

    total = len(cases)
    tp = sum(s == "pass" for _, s in cases)
    ts = sum(s == "skip" for _, s in cases)
    tf = sum(s == "fail" for _, s in cases)
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    commit = run(["git", "rev-parse", "--short", "HEAD"]).stdout.strip() or "unknown"
    if run(["git", "status", "--porcelain"]).stdout.strip():
        commit += " plus uncommitted changes"

    md = f"""# Test Results: Private AI Agent Template

Generated by `make test` on {now}, at commit {commit}, with Python {platform.python_version()} on {platform.system()}.
Do not edit by hand. Run `make test` to regenerate it.

**Automated suite: {tp} passed, {tf} failed, {ts} skipped, {total} total.**

## How to read this

Every automated check here ran against **stand-in servers**: small local programs that play the local
model and the frontier API over real network sockets. That makes the privacy and permission logic
testable, including connection counts, but it says nothing about how a real model behaves. The checks
that need a real GPU host are listed after the table and are **not run**. They are not counted as passing.

## Automated checks

| Check | Method | Result | Pass/Fail |
|---|---|---|---|
{chr(10).join(table)}

## Not run: needs a real GPU host

| Check | Method | Where | Status |
|---|---|---|---|
| All 10 no-answer questions, including the 4 near-miss ones, return "not in documents" | Real model following the grounding instruction (`tests/rag/report.py --live`) | RUNBOOK 6.6 | PARTIAL: {gated} verified offline; requirement not yet met |
""" + "\n".join(f"| {a} | {b} | {c} | NOT RUN |" for a, b, c in PENDING) + f"""

## Notes

1. **Retrieval numbers come from an offline stand-in embedder.** It matches words, not meaning. Recall@5
   {recall} and MRR {mrr} say nothing about the real embedding model, which should do better on paraphrases.
2. **The "not in documents" gate is only a first filter.** It closed on {gated} no-answer questions at the shipped
   floor. The six off-topic questions are all caught. The near-miss questions (topic present, fact absent)
   are not separable by a score floor with this embedder: raising it enough to catch them rejects most answerable
   questions. Those cases rely on the local model following its instruction to reply `not in documents`, which is
   **unverified** until a real model is run. False abstentions at the shipped floor: {falsea}.
3. **Faithfulness is not measured.** It needs a real model. The harness and the live mode exist and their plumbing is tested.
4. **The tool-use score is not measured for any real model.** The scorer works and the 20 tasks are solvable from real
   tool output, but the only score so far is a scripted stand-in. Raw self-check output:

```
{tool.strip()}
```

5. **OpenClaw has not been run.** Its configuration was written from documentation and is unverified.
"""
    OUT.write_text(md)
    print(f"wrote {OUT.relative_to(ROOT)}: {tp} passed, {tf} failed, {ts} skipped; rows failing: {failures}")
    return 0 if failures == 0 and tf == 0 and p.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
