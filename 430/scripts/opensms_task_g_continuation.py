"""Fail-closed, local-only gate for a user-authorized G pre-query continuation.

This is not a general retry: no prior G intent, batch or ledger change may exist.
Completed A writes and F import are never replayed by this continuation.
"""
from pathlib import Path
import hashlib
import json
import re


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_checkpoint(state_path, state, *, process_alive, settings_path, ledger_path):
    state_path = Path(state_path).resolve()
    run_id = str(state.get("runId") or "")
    delivery = Path(state.get("delivery") or "").resolve()
    if (not re.fullmatch(r"\d{8}T\d{6}\+0800", run_id)
            or delivery.name != run_id or state_path.parent != delivery / "receipts"
            or state_path.name != f"opensms_patrol_runner_state_{run_id}.json"):
        raise ValueError("TASK_G_CONTINUATION_STATE_IDENTITY_INVALID")
    if state.get("status") != "failed" or process_alive or state.get("taskGContinuationAttempts"):
        raise ValueError("TASK_G_CONTINUATION_NOT_STOPPED_OR_ALREADY_ATTEMPTED")
    if (state.get("error") != "任务G未完整确认，禁止重试：TASK_G_PHONE_IDENTITY_AMBIGUOUS"
            or (state.get("apiTarget") or {}).get("status") != "closed"
            or state.get("apiTargetCleanupError") or state.get("taskAOnly")
            or any((state.get("authorization") or {}).get("task" + name) for name in "BCDEF")
            or state.get("unexpiredRestorePlan")):
        raise ValueError("TASK_G_CONTINUATION_SCOPE_OR_FAILURE_UNCONFIRMED")
    stages = state.get("stages") or {}
    for name in ("acquire", "candidates", "document", "standardWrites", "taskF", "taskD", "taskE", "expiredCardProbe", "refreshBeforeTaskG", "taskG"):
        if not (stages.get(name) or {}).get("completedAt"):
            raise ValueError("TASK_G_CONTINUATION_PRIOR_STAGE_INCOMPLETE")
    if any(name in stages for name in ("refresh", "finalize")):
        raise ValueError("TASK_G_CONTINUATION_FINAL_STAGE_ALREADY_EXISTS")
    imported = state.get("fDateImport") or {}
    if imported.get("submitted") and imported.get("independentReadbackPassed") is not True:
        raise ValueError("TASK_G_CONTINUATION_CURRENT_IMPORT_UNCONFIRMED")
    if imported.get("status") in ("submission_outcome_uncertain", "accepted_pending_readback"):
        raise ValueError("TASK_G_CONTINUATION_CURRENT_IMPORT_UNCONFIRMED")
    if imported.get("status") == "failed_before_submission" and imported.get("submitted") is not False:
        raise ValueError("TASK_G_CONTINUATION_CURRENT_IMPORT_UNCONFIRMED")
    g = stages["taskG"]
    receipt_path = Path(g.get("receipt") or "")
    input_path = Path(g.get("input") or "")
    if (not receipt_path.is_file() or not input_path.is_file()
            or sha(receipt_path) != g.get("receiptSha256")
            or sha(input_path) != g.get("inputSha256")):
        raise ValueError("TASK_G_CONTINUATION_G_EVIDENCE_CHANGED")
    receipt, spec = load(receipt_path), load(input_path)
    if (receipt.get("runId") != run_id or spec.get("runId") != run_id
            or receipt.get("inputSha256") != sha(input_path)
            or receipt.get("status") != "failed_no_replay"
            or receipt.get("error") != "TASK_G_PHONE_IDENTITY_AMBIGUOUS"
            or receipt.get("batches") != [] or receipt.get("reconciliations") != []
            or receipt.get("dateChecks") != [] or receipt.get("skips") != []
            or receipt.get("enteredCount") != 0 or receipt.get("restoredCount") != 0
            or "ledgerAfter" in receipt or "ledgerAfterSha256" in receipt):
        raise ValueError("TASK_G_CONTINUATION_SUBMISSION_OR_LEDGER_CHANGE_POSSIBLE")
    ledger_path = Path(ledger_path).resolve()
    if (Path(receipt.get("ledgerPath") or "").resolve() != ledger_path
            or Path(str(ledger_path) + ".lock").exists()
            or not ledger_path.is_file()):
        raise ValueError("TASK_G_CONTINUATION_LEDGER_UNAVAILABLE")
    ledger = load(ledger_path)
    if (ledger != receipt.get("ledgerBefore") or ledger.get("version") != 1
            or not isinstance(ledger.get("records"), dict)
            or any(r.get("status") in ("entering", "restoring") for r in ledger["records"].values())):
        raise ValueError("TASK_G_CONTINUATION_LEDGER_CHANGED_OR_PENDING")
    if sha(settings_path) != spec.get("settingsSha256") or load(settings_path) != spec.get("settings"):
        raise ValueError("TASK_G_CONTINUATION_SETTINGS_CHANGED")
    for field in ("source", "scanCache", "postBusinessWorkbook", "postBusinessReadback"):
        file = Path(spec.get(field) or "")
        if not file.is_file() or sha(file) != spec.get(field + "Sha256"):
            raise ValueError("TASK_G_CONTINUATION_SOURCE_CHANGED")
    acquired = stages["acquire"]
    if (acquired.get("serverDateVerified") is not True or acquired.get("serverDate") != spec.get("serverDate")
            or sha(acquired["sourceArchive"]) != acquired.get("sourceSha256")
            or sha(acquired["processingSource"]) != acquired.get("sourceSha256")):
        raise ValueError("TASK_G_CONTINUATION_ACQUISITION_UNCONFIRMED")
    for group in (state.get("writeGroups") or {}).values():
        if group.get("result") not in ("passed", "no_candidate"):
            raise ValueError("TASK_G_CONTINUATION_PRIOR_WRITE_UNCONFIRMED")
        if group.get("result") == "passed":
            if load(group["verify"]).get("passed") is not True:
                raise ValueError("TASK_G_CONTINUATION_PRIOR_WRITE_UNCONFIRMED")
            for field in ("validated", "write"):
                if not Path(group[field]).is_file():
                    raise ValueError("TASK_G_CONTINUATION_PRIOR_WRITE_EVIDENCE_MISSING")
    before = stages["refreshBeforeTaskG"]
    if (sha(before["finalWorkbook"]) != before.get("finalWorkbookSha256")
            or load(before["finalStateReceipt"]).get("passed") is not True):
        raise ValueError("TASK_G_CONTINUATION_PREVIOUS_READBACK_UNCONFIRMED")
    for field in ("analysis", "routingManifest"):
        if not Path(stages["candidates"][field]).is_file():
            raise ValueError("TASK_G_CONTINUATION_PRIOR_REPORT_INPUT_MISSING")
    for field in ("cardResult", "cardReceipt"):
        if not Path(stages["document"][field]).is_file():
            raise ValueError("TASK_G_CONTINUATION_PRIOR_REPORT_INPUT_MISSING")
    return {"runId": run_id, "serverDate": spec["serverDate"],
            "receiptSha256": sha(receipt_path), "inputSha256": sha(input_path),
            "ledgerSha256": sha(ledger_path), "exportReplayed": False,
            "completedBusinessStagesReplayed": False, "fImportReplayed": False}
