# Decisions (draft)

1. **Indexes.** I use unique indexes on `employees.emp_code` and `attendance_logs(emp_code, date)` to enforce the identifiers and prevent duplicate daily records. Supporting indexes cover department/join-date filters, attendance date and status filters, and an employee's punch-in history. At 100,000 records, each explain plan used indexed access without a collection scan.

2. **Punch-in race.** When identical punch-ins arrive together, both try to insert. The unique attendance key lets one succeed; the other receives 409 instead of creating a duplicate.

3. **Leaderboard ties.** Ranking is by total late minutes, using competition ranks. Employees tied at the limit are all returned, in employee-code order.

4. **Department headcount.** I count eligible employees from `employees`, including people with no attendance logs, and exclude anyone whose join date is after month end.

5. **At 100x scale.** I would rerun correctness, latency, and explain checks with representative data before changing the design. The 100,000-record run was quick locally, but that does not predict 10 million records. I would first profile the slowest query and its indexes; if analytics became a bottleneck, I would consider pre-aggregated summaries. I would not add indexes or sharding without evidence that they address the measured bottleneck.
