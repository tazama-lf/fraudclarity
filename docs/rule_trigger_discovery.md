# Rule Trigger Discovery

**April 2026**

2. # Tazama Background {#tazama-background}

Tazama is a transaction monitoring system that detects suspicious financial behavior such as fraud, money laundering, and other illicit activities.  Tazama is built to analyze transaction data in real time, applying rules and typologies to identify outliers and patterns that may signal financial crime.

Tazama utilizes a deterministic, rules-based approach to transaction monitoring, prioritizing transparency, simplicity, and auditability. The system functions as a forward-chaining inference engine, implementing a variation of the Rete algorithm where each component performs a specific task and passes its results downstream to reach a final conclusion. This modular architecture currently supports 33 defined rules, each maintained in its own dedicated repository within the private frmscoe GitHub organization to prevent reverse engineering. With the introduction of Tazama Rule Studio in release 4.0.0, operators gain configurable flexibility to create and deploy new rules into their own repositories.

When a transaction is ingested via the Transaction Monitoring Service (TMS) API, the Event Director uses a configurable network map to determine which typologies apply and routes the transaction to the necessary rule processors. Each rule processor is a specialized component designed to evaluate a single, distinct behavior, such as account age, transaction velocity, or unusual counterparty patterns. To ensure consistency across the system, every processor uses a standard rule executer wrapper that handles data input, logging, and output, while the unique behavioral logic is contained within the rule module.

The rule module is structured around a specific database query, such as the logic found in rule-001.ts, which retrieves the historical behavioral data of the transaction participants. This query calculates an "independent variable," which is the raw numerical result of the evaluation, such as the specific number of transactions identified in a debtor's history. This value is then categorized into pre-defined risk levels (e.g., "1 to 3 months old") based on the individual rule's configuration. Because the "query IS the rule," the decision-making process remains highly interpretable and auditable for investigators and regulators.

After evaluation, these categorized results are sent to the Typology Processor, where they are weighted and scored collectively to identify specific fraud or money-laundering scenarios. The processor compares the combined sum of these weightings against two configurable thresholds: an alert threshold for generating investigation alerts and an interdiction threshold for immediate transaction blocking. The final stage of the engine is the Transaction Aggregation and Decisioning Processor (TADProc), which consolidates results from all evaluated typologies into a single, comprehensive evaluation report. The evaluation report contains details of the alert status, original transaction, network map, typology and rule results. This evaluation report is recorded in the database and can be routed to the Case Management System as a JSON object for further action.

Within the Case and Investigation Management System, an investigator would then be tasked to investigate the alert to firstly confirm if the alert is valid and, if so, to conduct a fraud or money-laundering investigation and take actions in support of the operator's compliance obligations.

The Alert Navigator provides a visual overview of a specific transaction alert, detailing metadata such typologies and rules evaluated during processing and displays evaluation results information, including rule weightings and independent variables alongside the thresholds that determine whether a transaction is flagged for investigation or immediately interdicted. Through the "rule trigger discovery" enhancement, the visualization further allows investigators to drill down into these results to view the specific constituent transactions or behavioral attributes, such as account age or geographic distance, that caused the alert trigger.

4. # Rule Queries {#rule-queries}

This section documents the database query contained in each rule module, including the transaction attributes used as inputs and the value returned. The query is the core logic of each rule and determines the independent variable passed to the outcome determination function.

1. # Rule-001: Creditor Account Age {#rule-001:-creditor-account-age}

**Rule name:** Age of creditor account based on oldest verifiable transaction activity

**Query inputs (filter attributes read from the \`transaction\` table):**

| Attribute | Usage |
| :---- | :---- |
| source | Filters to the creditor account ID (cdtrAcctId) \- identifies which account's transactions to evaluate |
| TxTp | Transaction type \- filters for pacs.008.001.10 (credit transfer initiation) or pacs.002.001.12 (payment status report) |
| TxSts | Transaction status \- filters for ACCC (Accepted Credit Completed) to identify confirmed successful payments |
| CreDtTm | Creation datetime \- used to filter transactions up to and including the alerting transaction's CreDtTm (sourced from the evaluation report), and to find the earliest (MIN) timestamps |
| EndToEndId | End-to-end transaction ID \- used to join the earliest successful pacs.002 status back to its originating pacs.008 credit transfer |
| TenantId | Tenant identifier \- scopes all query filters to the correct tenant |

**Returned column:**

| Attribute | Description |
| :---- | :---- |
| oldestCreDtTm | The CreDtTm of the earliest verifiable transaction associated with the creditor account \- specifically the lesser of (a) the oldest sent pacs.008 and (b) the oldest pacs.008 linked to a confirmed successful pacs.002. Returns NULL if no qualifying activity exists. |

**Summary:** The query determines the age of the creditor account by identifying the timestamp of its oldest verifiable transaction activity. It finds the earliest outgoing credit transfer (pacs.008) sent to the account and the earliest confirmed successful credit transfer (via a pacs.002 ACCC status match on EndToEndId), then returns whichever timestamp is earlier. The time difference in milliseconds between the oldest activity and the current transaction is passed to the outcome function and banded into a risk category \- flagging potentially new or synthetic creditor accounts.

2. # Rule-002: Number of Successful Debtor Transactions Within a Time Window {#rule-002:-number-of-successful-debtor-transactions-within-a-time-window}

**Rule name:** Count of successful outgoing transactions made by the debtor within a configurable lookback period

**Query inputs (filter attributes read from the \`transaction\` table):**

| Attribute | Usage |
| :---- | :---- |
| source | Filters to the debtor account ID (dbtrAcctId) \- identifies which account's transactions to count |
| TxTp | Transaction type \- filters for pacs.002.001.12 (payment status report) only |
| TxSts | Transaction status \- filters for ACCC (Accepted Credit Completed) to count only confirmed successful transactions |
| CreDtTm | Creation datetime \- used to constrain results to a rolling time window: from (pacs002CreDtTm − maxQueryRange ms) up to and including pacs002CreDtTm |
| TenantId | Tenant identifier \- scopes the query to the correct tenant |

**Query parameter:**

| Parameter | Usage |
| :---- | :---- |
| maxQueryRange | Configurable lookback window in milliseconds \- defines how far back in time the count of successful transactions is measured |

**Note:** maxQueryRange is sourced from the rule configuration (ruleConfig.config.parameters.maxQueryRange), which is loaded by the rule executer from the configuration database at startup. The rule configuration is operator-defined and associated with the rule via the network map.

**Open questions:**

1\. Is the history of rule configurations available in the Data Lakehouse, so that the correct configuration value (e.g. maxQueryRange) at the time of the original alert can be retrieved and applied when re-running the query for visualization purposes?

**Returned column:**

| Attribute | Description |
| :---- | :---- |
| length | A count (COUNT(\*)) of successful pacs.002 ACCC transactions associated with the debtor account within the configured time window, returned as a bigint |

**Summary:** The query counts the number of confirmed successful outgoing transactions (pacs.002 ACCC) made by the debtor account within a configurable rolling lookback window (defined in milliseconds by maxQueryRange). The result is a single integer count which is passed directly to the outcome function and banded into a risk category \- identifying debtors with unusually high or low transaction volumes over the defined period.

3. # Rule-003: Time Since Most Recent Creditor Account Activity {#rule-003:-time-since-most-recent-creditor-account-activity}

**Rule name:** Time elapsed since the most recent verifiable transaction activity on the creditor account, excluding the current transaction

**Query inputs (filter attributes read from the \`transaction\` table):**

| Attribute | Usage |
| :---- | :---- |
| source | Filters to the creditor account ID (cdtrAcctId) \- identifies which account's transactions to evaluate |
| TxTp | Transaction type \- filters for pacs.008.001.10 (credit transfer initiation) or pacs.002.001.12 (payment status report) |
| TxSts | Transaction status \- filters for ACCC (Accepted Credit Completed) to identify confirmed successful payments |
| EndToEndId | End-to-end transaction ID \- used to **exclude** the current transaction from the results, and to join the most recent successful pacs.002 back to its originating pacs.008 |
| CreDtTm | Creation datetime \- used to filter transactions strictly before the current transaction timestamp, and to find the latest (MAX) timestamps |
| TenantId | Tenant identifier \- scopes all query filters to the correct tenant |

**Returned column:**

| Attribute | Description |
| :---- | :---- |
| maxCreDtTm | The CreDtTm of the most recent verifiable transaction associated with the creditor account \- specifically the greater of (a) the most recent sent pacs.008 and (b) the most recent pacs.008 linked to the latest confirmed successful pacs.002, both excluding the current transaction. Returns NULL if no prior activity exists. |

**Summary:** The query determines how recently the creditor account was last active by finding the timestamp of its most recent prior transaction, explicitly excluding the current transaction via EndToEndId. It finds the most recent outgoing credit transfer (pacs.008) sent to the account and the most recent confirmed successful credit transfer (via a pacs.002 ACCC status match on EndToEndId), then returns whichever timestamp is later. The time difference in milliseconds between the most recent prior activity and the current wall-clock time (Date.now()) is passed to the outcome function and banded into a risk category \- flagging creditor accounts that have been dormant for an unusually long or short period.

**Note:** Unlike Rule-001 which measures account age from the *oldest* activity, Rule-003 measures time since the *most recent* activity. In real-time execution the rule calculates elapsed time against the current wall-clock time (Date.now()). At the moment the rule fires these two values are essentially equivalent. However, for a retrospective visualization replay, Date.now() returns the current date rather than the alert date, producing an incorrect elapsed time. For visualization purposes, the alerting transaction's CreDtTm (available in the evaluation report) must be used as the reference timestamp in place of Date.now() to faithfully reproduce the elapsed time value that the rule computed at the time of the alert.

4. # Rule-004: Time Since Most Recent Debtor Account Activity

**Rule name:** Time elapsed since the most recent verifiable transaction activity on the debtor account, excluding the current transaction

**Query inputs (filter attributes read from the \`transaction\` table):**

| Attribute | Usage |
| :---- | :---- |
| source | Filters to the debtor account ID (dbtrAcctId) \- identifies which account's transactions to evaluate |
| TxTp | Transaction type \- filters for pacs.008.001.10 (credit transfer initiation) or pacs.002.001.12 (payment status report) |
| TxSts | Transaction status \- filters for ACCC (Accepted Credit Completed) to identify confirmed successful payments |
| EndToEndId | End-to-end transaction ID \- used to **exclude** the current transaction from the results, and to join the most recent successful pacs.002 back to its originating pacs.008 |
| CreDtTm | Creation datetime \- used to filter transactions strictly before the current transaction timestamp, and to find the latest (MAX) timestamps |
| TenantId | Tenant identifier \- scopes all query filters to the correct tenant |

**Returned column:**

| Attribute | Description |
| :---- | :---- |
| maxCreDtTm | The CreDtTm of the most recent verifiable transaction associated with the debtor account \- specifically the greater of (a) the most recent sent pacs.008 and (b) the most recent pacs.008 linked to the latest confirmed successful pacs.002, both excluding the current transaction. Returns NULL if no prior activity exists. |

**Summary:** The query determines how recently the debtor account was last active by finding the timestamp of its most recent prior transaction, explicitly excluding the current transaction via EndToEndId. It finds the most recent outgoing credit transfer (pacs.008) from the debtor account and the most recent confirmed successful credit transfer (via a pacs.002 ACCC status match on EndToEndId), then returns whichever timestamp is later. The time difference in milliseconds between the most recent prior activity and the current wall-clock time (Date.now()) is passed to the outcome function and banded into a risk category \- flagging debtor accounts that have been dormant for an unusually long or short period.

**Note:** Rule-004 is the debtor-account equivalent of Rule-003. Both rules use the same query structure and measure time since most recent activity against Date.now(), but Rule-003 queries by cdtrAcctId while Rule-004 queries by dbtrAcctId. In real-time execution the rule calculates elapsed time against the current wall-clock time (Date.now()). At the moment the rule fires these two values are essentially equivalent. However, for a retrospective visualization replay, Date.now() returns the current date rather than the alert date, producing an incorrect elapsed time. For visualization purposes, the alerting transaction's CreDtTm (available in the evaluation report) must be used as the reference timestamp in place of Date.now() to faithfully reproduce the elapsed time value that the rule computed at the time of the alert.

5. # Rule-006: Number of Similar-Amount Transactions to the Debtor Account {#rule-006:-number-of-similar-amount-transactions-to-the-debtor-account}

**Rule name:** Count of historical incoming transactions to the debtor account with amounts similar to the current transaction, within a configurable limit

**Query inputs (filter attributes read from the \`transaction\` table):**

| Attribute | Usage |
| :---- | :---- |
| destination | Filters to the debtor account ID (dbtrAcctId) \- identifies the account receiving the transactions |
| TxTp | Transaction type \- filters for pacs.002.001.12 (payment status report) in the subquery to identify successful transactions, and pacs.008.001.10 (credit transfer) in the main query to retrieve the transaction amounts |
| TxSts | Transaction status \- filters for ACCC (Accepted Credit Completed) in the subquery to include only confirmed successful incoming transactions |
| CreDtTm | Creation datetime \- used to constrain results to transactions up to and including the current transaction timestamp |
| EndToEndId | End-to-end transaction ID \- used to JOIN confirmed successful pacs.002 records back to their originating pacs.008 credit transfers to retrieve the transaction amounts |
| TenantId | Tenant identifier \- scopes all query filters to the correct tenant |

**Query parameters:**

| Parameter | Usage |
| :---- | :---- |
| maxQueryLimit | Configurable cap on the number of most recent transactions to retrieve and evaluate \- sourced from the rule configuration |
| tolerance | Fractional tolerance band used to determine whether a historical transaction amount is considered "similar" to the current transaction amount (e.g. 0.1 \= within 10%) \- sourced from the rule configuration |

**Note:** Both maxQueryLimit and tolerance are sourced from the rule configuration (ruleConfig.config.parameters), which is loaded by the rule executer from the configuration database at startup and associated with the rule via the network map.

**Returned column:**

| Attribute | Description |
| :---- | :---- |
| Amt | The transaction amount for each of the most recent maxQueryLimit successful incoming pacs.008 transactions to the debtor account. Returns one row per transaction up to the configured limit. |

**Post-query processing:** The rule does not return a single scalar from the database. Instead, it retrieves up to maxQueryLimit amounts and then counts how many fall within the tolerance band of the current transaction's amount using the countMatchingAmounts function. That count is the independent variable passed to determineOutcome.

**Summary:** The query retrieves the amounts of the most recent confirmed successful incoming transactions to the debtor account (up to maxQueryLimit), by joining successful pacs.002 ACCC records back to their originating pacs.008 amounts via EndToEndId. The rule then counts how many of those historical amounts fall within a configurable tolerance band of the current transaction's amount. The resulting count is banded into a risk category \- flagging accounts that repeatedly receive transactions of suspiciously similar amounts, a pattern associated with structuring or layering.

6. # Rule-007: Similarity of Remittance Information for the Two Most Recent Incoming Transactions {#rule-007:-similarity-of-remittance-information-for-the-two-most-recent-incoming-transactions}

**Rule name:** Levenshtein distance between the unstructured remittance information of the two most recent confirmed incoming transactions to the debtor account

**Query overview:** Rule-007 executes two sequential queries against two separate databases: an initial query against the \_eventHistory pool (the transaction table) to identify the two most recent successful incoming transactions, followed by a second query against the \_rawHistory pool (the pacs008 raw document store) to retrieve the remittance information text from each.

**Query 1 \- identify most recent incoming transactions**

Query inputs (filter attributes read from the transaction table):

| Attribute | Usage |
| :---- | :---- |
| destination | Filters to the debtor account ID (dbtrAcctId) \- identifies the account receiving the transactions |
| TxTp | Transaction type \- filters for pacs.002.001.12 (payment status report) |
| TxSts | Transaction status \- filters for ACCC (Accepted Credit Completed) to include only confirmed successful incoming transactions |
| CreDtTm | Creation datetime \- used to constrain results to transactions up to and including the current transaction timestamp; results ordered newest-first |
| TenantId | Tenant identifier \- scopes all query filters to the correct tenant |

**Returned column:**

| Attribute | Description |
| :---- | :---- |
| EndToEndId | The end-to-end transaction identifier for each of the two most recent successful incoming pacs.002 ACCC transactions to the debtor account (LIMIT 2\) |

**Query 2 \- retrieve remittance information**

Query inputs (filter attributes read from the pacs008 raw document store):

| Attribute | Usage |
| :---- | :---- |
| EndToEndId\[\] | Array of two EndToEndId values returned by Query 1 \- used to match and retrieve the originating pacs.008 credit transfer documents |
| tenantId | Tenant identifier \- scopes the query to the correct tenant |

**Returned column:**

| Attribute | Description |
| :---- | :---- |
| Ustrd | The unstructured remittance information (RmtInf.Ustrd) from each originating pacs.008 credit transfer document \- the free-text payment reference string |

**Post-query processing:** The rule calculates the Levenshtein edit distance between the two Ustrd strings. The resulting integer distance is the independent variable passed to determineOutcome.

**Summary:** The query identifies the two most recent confirmed incoming transactions to the debtor account and retrieves the free-text payment reference (Ustrd) from each originating pacs.008 credit transfer. The Levenshtein edit distance between the two reference strings is then calculated \- a low distance (high similarity) indicates the payment references are nearly identical, which may signal repeated or scripted transfers. This is banded into a risk category flagging accounts that repeatedly receive payments with suspiciously similar payment references, a pattern associated with structuring or automated fraud.

**Note:** Ustrd is not a column in the transaction table and must be retrieved from the pacs008 raw document store using the EndToEndId as the join key.

7. # Rule-008: Count of Recent Incoming Transactions from the Same Creditor {#rule-008:-count-of-recent-incoming-transactions-from-the-same-creditor}

**Rule name:** Count of the most recent confirmed incoming transactions to the debtor account that originated from the same creditor as the most recent transaction

**Query inputs (filter attributes read from the \`transaction\` table):**

| Attribute | Usage |
| :---- | :---- |
| destination | Filters to the debtor account ID (dbtrAcctId) \- identifies the account receiving the transactions |
| TxTp | Transaction type \- filters for pacs.002.001.12 (payment status report) |
| TxSts | Transaction status \- filters for ACCC (Accepted Credit Completed) to include only confirmed successful incoming transactions |
| CreDtTm | Creation datetime \- used to constrain results to transactions up to and including the current transaction timestamp; results ordered newest-first |
| TenantId | Tenant identifier \- scopes all query filters to the correct tenant |

**Query parameter:**

| Parameter | Usage |
| :---- | :---- |
| maxQueryLimit | Optional configurable cap on the number of most recent incoming transactions to retrieve \- sourced from the rule configuration. If not configured, defaults to 3 in the exit condition check. |

**Note:** maxQueryLimit is sourced from the rule configuration (ruleConfig.config.parameters.maxQueryLimit), which is loaded by the rule executer from the configuration database at startup and associated with the rule via the network map.

**Returned column:**

| Attribute | Description |
| :---- | :---- |
| source | The creditor account ID (cdtrAcctId) recorded in each pacs.002 transaction row \- identifies who sent each of the most recent confirmed incoming transactions to the debtor account. Returns one row per transaction up to maxQueryLimit. |

**Note:** In the transaction table, for a pacs.002 record, source holds the creditor account ID and destination holds the debtor account ID \- i.e. the source/destination orientation follows the direction of the original pacs.008 credit transfer, not the pacs.002 status report itself.

**Post-query processing:** The rule takes the source value from the most recent row (the most recent incoming transaction's creditor) and counts how many of the other returned rows share the same source value. That count is the independent variable passed to determineOutcome.

**Summary:** The query retrieves the creditor account IDs of the most recent confirmed successful incoming transactions to the debtor account (up to maxQueryLimit). The rule then counts how many of those transactions originated from the same creditor as the most recent one. A high count indicates that the debtor has repeatedly received funds from the same counterparty in their most recent transaction history \- a pattern that may signal funneling, layering, or a controlled transaction network.

8. # Rule-010: Variance in Incoming Transaction Frequency Over Configurable Time Intervals {#rule-010:-variance-in-incoming-transaction-frequency-over-configurable-time-intervals}

**Rule name:** Statistical variance in the number of confirmed incoming transactions to the debtor account per configurable time interval, relative to historical frequency

**Query inputs (filter attributes read from the \`transaction\` table):**

| Attribute | Usage |
| :---- | :---- |
| destination | Filters to the debtor account ID (dbtrAcctId) \- identifies the account receiving the transactions |
| TxTp | Transaction type \- filters for pacs.002.001.12 (payment status report) |
| TxSts | Transaction status \- filters for ACCC (Accepted Credit Completed) to include only confirmed successful incoming transactions |
| CreDtTm | Creation datetime \- used to constrain results to transactions up to and including the current transaction timestamp; results ordered newest-first |
| EndToEndId | End-to-end transaction ID \- used to locate the current transaction within the result set to anchor the histogram |
| TenantId | Tenant identifier \- scopes all query filters to the correct tenant |

**Query parameter:**

| Parameter | Usage |
| :---- | :---- |
| evaluationIntervalTime | The width of each histogram bucket in milliseconds \- defines the time period used to group transactions when calculating transaction frequency. Sourced from the rule configuration. |

**Note:** evaluationIntervalTime is sourced from the rule configuration (ruleConfig.config.parameters.evaluationIntervalTime) and must be provided \- the rule throws an error if it is absent.

**Returned columns:**

| Attribute | Description |
| :---- | :---- |
| EndToEndId | The end-to-end transaction identifier for each historical incoming transaction |
| CreDtTm | The creation timestamp for each historical incoming transaction \- used to bin transactions into time intervals for the histogram |

**Post-query processing:** The rule builds a histogram of confirmed incoming transactions per evaluationIntervalTime window across the full result set. It then:

1. Removes the most recent interval (the "current" bucket) from the histogram  
2. Calculates the mean and standard deviation of transaction counts across the remaining historical intervals  
3. Re-scales the rule's band limits using scaledLimit \= configuredLimit × stdDev \+ avg  
4. Passes the count of transactions in the most recent interval as the independent variable to determineOutcome

**Summary:** The query retrieves the full history of confirmed successful incoming transactions to the debtor account (pacs.002 ACCC) up to and including the current transaction. The rule builds a histogram bucketing these transactions by a configurable time interval (evaluationIntervalTime), calculates the mean and standard deviation of per-interval counts across the historical data, and assesses whether the count in the current interval is statistically anomalous. The band thresholds are scaled dynamically relative to the account's own historical variance, making the rule self-calibrating. It flags debtor accounts exhibiting a sudden spike in incoming transaction frequency relative to their own baseline \- a pattern associated with rapid fund accumulation prior to layering or withdrawal.

9. # Further Rule Query Specifications {#further-rule-query-specifications}

Query specifications for the following rules are still to be documented. Each will follow the same structure as the rules in Section 5, covering query inputs, returned columns, post-query processing, summary, and additional visualization requirements. Rule names are sourced from the repository descriptions in the frmscoe GitHub organisation.

| Rule | Rule Name |
| :---- | :---- |
| Rule-011 | Increased account activity: volume — creditor |
| Rule-016 | Transaction convergence — creditor |
| Rule-017 | Transaction divergence — debtor |
| Rule-018 | Exceptionally large outgoing transfer — debtor |
| Rule-020 | Large transaction amount vs history — creditor |
| Rule-021 | A large number of similar transaction amounts — creditor |
| Rule-024 | Non-commissioned transaction mirroring — creditor |
| Rule-025 | Non-commissioned transaction mirroring — debtor |
| Rule-026 | Commissioned transaction mirroring — creditor |
| Rule-027 | Commissioned transaction mirroring — debtor |
| Rule-028 | Age classification — debtor |
| Rule-030 | Transfer to unfamiliar creditor account — debtor |
| Rule-044 | Successful transactions from the debtor, including the new transaction |
| Rule-045 | Successful transactions to the creditor, including the new transaction |
| Rule-048 | Large transaction amount vs history — debtor |
| Rule-054 | Synthetic data check — Benford's Law — debtor |
| Rule-063 | Synthetic data check — Benford's Law — creditor |
| Rule-074 | Distance over time from last transaction location — debtor |
| Rule-075 | Distance from habitual locations — debtor |
| Rule-076 | Time since last transaction — debtor |
| Rule-078 | Transaction type |
| Rule-083 | Multiple accounts associated with a debtor |
| Rule-084 | Multiple accounts associated with a creditor |
| Rule-090 | Upstream transaction divergence — debtor |
| Rule-091 | Transaction amount vs regulatory threshold |

[image1]: <data:image/png;base64,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>