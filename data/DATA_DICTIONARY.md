# Data Dictionary

## Feature columns
| Column | Meaning |
|---|---|
| transaction_id | Synthetic unique transaction identifier |
| timestamp | Transaction event time |
| customer_id | Synthetic customer identifier |
| merchant_id | Synthetic merchant identifier |
| device_id | Synthetic device identifier |
| ip_cluster_id | Synthetic coarse network-cluster identifier |
| amount_inr | Transaction amount in INR |
| merchant_category | Synthetic merchant category |
| payment_method | UPI, card, netbanking, or wallet |
| channel | Android, iOS, or web |
| customer_age_days | Age of customer relationship/account |
| customer_txn_count_30d | Historical 30-day transaction count |
| customer_avg_amount_30d | Historical mean transaction amount |
| time_since_last_txn_min | Minutes since previous transaction for customer |
| txn_velocity_10m | Recent short-window activity count |
| txn_velocity_1h | Recent one-hour activity count |
| amount_zscore_customer | Amount deviation from customer baseline |
| is_new_device | Whether device is new for customer |
| device_trust_score | Synthetic device trust score [0,1] |
| ip_risk_score | Synthetic network risk score [0,1] |
| geo_distance_km | Distance from typical customer geography |
| billing_shipping_mismatch | Synthetic mismatch indicator |
| failed_attempts_24h | Recent failed-attempt count |
| account_changes_24h | Recent account-change count |
| is_proxy_ip | Synthetic proxy/network-risk indicator |
| prior_disputes_90d | Prior dispute count |
| merchant_fraud_rate_7d | Synthetic historical merchant risk rate |
| known_promo_event | Known legitimate promotion context |
| hour | Hour of day |
| day_of_week | Monday=0 ... Sunday=6 |
| is_weekend | Weekend indicator |

## Label/evaluation-only columns
| Column | Meaning |
|---|---|
| is_fraud | Ground-truth transaction fraud label |
| is_spike_injected | Transaction belongs to injected fraud-campaign traffic |
| injected_spike_event_id | Injected campaign identifier |
| benign_event_id | Known benign surge identifier |
| is_within_spike_window | Timestamp lies inside a ground-truth fraud-spike window |
| active_spike_event_id | Ground-truth event active at this timestamp |
| fraud_scenario_family | Evaluation-only scenario family |
| false_positive_cost_if_blocked_inr | Synthetic cost if genuine transaction is falsely blocked |
| fraud_loss_if_missed_inr | Synthetic loss if fraudulent transaction is missed |

### Leakage warning
Do **not** use any label/evaluation-only field as a model input.
Identifiers should be handled carefully; raw high-cardinality IDs should not be treated as direct ordinal numeric features.
