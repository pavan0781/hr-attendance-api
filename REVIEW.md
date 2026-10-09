# Review

The original starter implementation was inspected against the assignment's API
and `DATA_MODEL.md`. The following 15 defects or explicit implementation-contract
violations were confirmed. Behavioral reproductions against that snapshot are in
the ignored `dev/starter_audit_test.py`; it starts the archived starter against a
random database and does not delete existing data. Pipeline and explain-builder
regressions are checked by `dev/pipeline_contract_test.py`. The race finding is
from the barrier-started race reproduction recorded below.

| # | Original location | Reproduction or confirmed symptom | Fix |
|---|---|---|---|
| 1 | `app/main.py` (`PunchInIn`, `PunchOutIn`; lines 542, 572) | POST a numeric-string epoch-millisecond `punched_at`, e.g. `"1780317000000"`. Pydantic coerced it and the starter returned 201; the contract requires 422 for strings and floats. | Validate timestamp values strictly before Pydantic can coerce them. |
| 2 | `app/main.py` (`PunchInIn`, `PunchOutIn`; lines 542, 572) | POST `{"emp_code":"EMP9002","punched_at":null}`. The starter treated explicit null as the omitted default and returned 201 instead of 422. | Reject an explicitly supplied null timestamp while retaining the omitted-time default. |
| 3 | `app/main.py` (`PunchInIn`, `PunchOutIn`, `RegularizeRequest`; lines 542, 572, 591) | Include `work_hours` in a punch-in request. The starter returned 422 because the models forbade the field; the contract says derived fields sent by clients are ignored. | Ignore only the documented derived fields before strict request validation. |
| 4 | `app/main.py` (`list_attendance`; line 751) | GET `/attendance?date_from=`. The starter returned 200 because a supplied empty value was treated as if the parameter were omitted; it must return 422. | Distinguish an omitted parameter from a supplied empty value and validate the latter. |
| 5 | `app/main.py` (`punch_out`; line 717 and timestamp conversion near line 35) | Seed `punch_in=2026-06-01T04:00:00.900Z`, then punch out at 10:30:18 IST. The starter calculated `work_hours=1.0`; truncating the stored punch-in first gives 3618 seconds, or 1.01 hours. | Normalize stored instants to whole seconds before derived calculations. |
| 6 | `app/main.py` (`serialize_attendance_record`; line 167) | Read a seeded record with `punch_in=2026-06-01T04:00:00.900Z`. The response exposed whole seconds while MongoDB retained `.900000`; normalization was response-only. | Persist normalization with conditional compare-and-set updates, retrying on concurrent changes. |
| 7 | `app/main.py` (`with_history_changes`, `serialize_history_entry`; lines 157, 433; PATCH at line 786) | PATCH an existing record's `punch_in`. The starter stored the nested `history.changes.punch_in.from` as an integer, then tried to serialize it as a BSON datetime and returned HTTP 500. The isolated baseline reproduction observed `Int64` in MongoDB and HTTP 500. | Persist timestamp values inside history as BSON datetimes and convert them only at serialization. |
| 8 | `app/main.py` (`employee_monthly_pipeline`; line 244; similar date expressions in the department pipelines) | Seed a `PRESENT` record for Monday `2026-06-01`. The starter returned `present_days=0.0`; parsing midnight with `+05:30` and then evaluating `$dayOfWeek` in UTC shifted the date to Sunday. | Derive weekdays from the calendar date without applying a timezone shift. |
| 9 | `app/main.py` (`department_summary_pipeline`; line 272) | Seed two employees in one department, with two June logs for one and none for the other. The starter returned headcount 3 instead of 2 because it summed unwound employee-log rows. | Count each eligible employee once, independently of log count. |
| 10 | `app/main.py` (`employee_monthly`; lines 875-915) | One Tuesday presence record in June (22 working days) produced `attendance_pct=4.55`; R8 requires four decimal places, so the expected value is 4.5455. The starter rounded this rate to two places in Python. | Calculate the rate in the aggregation and apply four-place half-up rounding. |
| 11 | `app/main.py` (`employee_monthly_pipeline` and `employee_monthly`; lines 244, 875-915) | The starter's monthly aggregation did not compute `working_days` or `attendance_pct`: the handler looped over dates in Python and calculated the rate after aggregation. This violated the required all-in-pipeline calculation even when sample values looked correct. | Compute working days and attendance rate in MongoDB. |
| 12 | `app/main.py` (`department_summary_pipeline` and `department_summary`; lines 272, 920-939) | The pipeline returned an hours total and count, then the handler calculated `avg_work_hours` in Python. This violated the required database-side metric computation and left the final calculation outside the explained query. | Compute the record-level average inside the aggregation pipeline. |
| 13 | `app/main.py` (`department_trend_pipeline`; lines 370-379) | The trend pipeline assembled every date with a Python `while` loop and `$documents`; the required `$densify` stage was absent. An 8-day trend request returned rows, but did not satisfy the required database-side gap filling. | Use `$densify` with an exclusive upper bound adjusted to include `to`. |
| 14 | `app/main.py` (`explain_endpoint`; line 996 and endpoint branches) | Explain for monthly/summary/leaderboard/trend used hand-built `find` queries rather than the same aggregation builders as the real handlers. The explain output therefore did not describe the endpoint's actual pipeline. | Reuse the endpoint query builders and explain those exact operations. |
| 15 | `app/main.py` (`punch_out` and `regularize_attendance`; lines 717, 786) | In the barrier-started punch-out versus PATCH(`punch_in`) race, both requests returned 200 but the final stored punch-in could differ from the successful PATCH audit entry. This was a lost update, not just a duplicate-request response issue. | Make punch-out conditional and atomic; use optimistic PATCH updates with retries. |

## Examined and fine

- In the post-fix code, a valid PATCH against an existing record returned HTTP 200;
  the HTTP 500 above is specific to the original starter's timestamp-change path.
- The current pipeline regression script verifies monthly, department-average,
  trend-gap-fill, and explain-builder behavior. The independent analytics and
  100,000-record scale checks are rerun separately; their latest results belong
  in the final audit report, not as a blanket claim that every environment is
  ready.
