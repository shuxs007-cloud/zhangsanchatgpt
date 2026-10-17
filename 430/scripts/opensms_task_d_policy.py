"""Current Task D population policy; historical v1 receipts stay readable.

Card-retention requests and the two notification branches are independent of
the deleted B14 carriers. No population totals (including fake zeros) exist in
the new contract. This module does not perform I/O or authorize business work.
"""
RETENTION_POPULATION_CONTRACT_VERSION = 2


def removed_population_fields():
    return {
        "retentionSummaryContractVersion": RETENTION_POPULATION_CONTRACT_VERSION,
        "retentionPopulationEnabled": False,
        "retentionPopulationStatus": "removed_by_user",
        "retentionPopulationReadExecuted": False,
        "retentionSummary": None,
        "retentionPopulationReceipt": None,
        "retentionSummaryReceipt": None,
        "retentionGroupsReceipt": None,
    }


def population_statistics_removed(value, *, report=False):
    keys = (("taskDRetentionSummaryContractVersion", "taskDRetentionPopulationEnabled",
             "taskDRetentionPopulationStatus") if report else
            ("retentionSummaryContractVersion", "retentionPopulationEnabled",
             "retentionPopulationStatus"))
    return (type(value.get(keys[0])) is int and value.get(keys[0]) == RETENTION_POPULATION_CONTRACT_VERSION
            and value.get(keys[1]) is False and value.get(keys[2]) == "removed_by_user")
