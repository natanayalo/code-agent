with open("apps/observability.py") as f:
    lines = f.readlines()

out = []
in_import = False
for line in lines:
    if line.startswith("from apps.observability_utils import ("):
        in_import = True
        out.append("from apps.observability_utils import (\n")
        out.append("    ATTEMPT_COUNT_ATTRIBUTE,\n")
        out.append("    ATTR_ROUTE_REASON,\n")
        out.append("    ATTR_TASK_KIND,\n")
        out.append("    ATTR_VERIFICATION_SUMMARY,\n")
        out.append("    ATTR_WORKER_ID,\n")
        out.append("    CHANNEL_ATTRIBUTE,\n")
        out.append("    INPUT_MIME_TYPE_ATTRIBUTE,\n")
        out.append("    INPUT_VALUE_ATTRIBUTE,\n")
        out.append("    MAX_SPAN_ATTRIBUTE_LENGTH,\n")
        out.append("    OPENINFERENCE_SPAN_KIND_ATTRIBUTE,\n")
        out.append("    OUTCOME_STATUS_ATTRIBUTE,\n")
        out.append("    OUTPUT_MIME_TYPE_ATTRIBUTE,\n")
        out.append("    OUTPUT_VALUE_ATTRIBUTE,\n")
        out.append("    SESSION_ID_ATTRIBUTE,\n")
        out.append("    TASK_ID_ATTRIBUTE,\n")
        out.append(")\n")
        continue
    if in_import:
        if line.strip() == ")":
            in_import = False
        continue
    out.append(line)

with open("apps/observability.py", "w") as f:
    f.writelines(out)
