# L3B Architecture Record

Tài liệu mô tả quyết định có thể kiểm chứng của workflow. Không ghi prompt bí mật hoặc
chain-of-thought vào output hay trace.

## 1. System overview

```text
Input ─► Entity agent (rule) ─► Coordinator evidence plan (MCP, song song, mỗi tool 1 lần)
                                   │
                                   ▼
                       scoped_evidence(): incident window, loại decoy
                                   │
          ┌────────────────────────┼────────────────────────┐
          ▼                        ▼                        ▼
   order-agent (LLM)      shipment-agent (LLM)      payment-agent (LLM)     ← chạy song song
          └───────── grounding: bỏ finding mâu thuẫn facts ─┘
                                   ▼
          Coordinator (LLM) chọn primary_issue trong allowed_issues
                                   ▼
            Verifier (rule engine độc lập) ── bất đồng ─► Coordinator xem lại 1 lần
                                   │            vẫn bất đồng ─► lấy issue của verifier, confidence 0.55
                                   ▼
          analyze_case(issue_override): policy, refund, entities ─► hard checks ─► Output
MCP evidence ──► case-local ledger ──► tool_result_consumed ──► Trace
```

LLM đề xuất issue và phải được verifier xác nhận. Khi LLM vẫn bất đồng sau lần xem lại,
case fail-closed về issue của verifier: trong lần chạy đầu tiên với 100 case, 17/17 lần LLM
đè kết quả của rule đều là false positive (LLM báo trễ hàng hoặc mismatch không có thật) và
đề xuất refund sai. Nếu LLM không trả lời được (lỗi API), case là `insufficient_evidence`.
Phần tính toán tài chính, policy mapping và entity được suy ra cơ học từ issue bằng code, để
tránh LLM bịa số tiền hoặc evidence ref.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer | case, customer hint, claimed order | Xác nhận claimed order có trong history của khách; reject `candidate-*` | `get_customer_history`, `get_order` | `ENTITY_RESOLVED` / `ENTITY_NOT_FOUND` |
| Coordinator | brief, findings, scoped evidence | Lập evidence plan; LLM chọn một `primary_issue` | Không gọi tool trực tiếp | `policy_decided` |
| Order/product | facts, incident order, items, payments | LLM, chỉ được trả `canceled_order_paid`, `unavailable_order_paid` hoặc `null` | `get_order_items` | finding `{issue, confidence}` |
| Shipment | facts, incident order, items, shipment events, shipping limits | LLM, chỉ được trả `late_delivery_seller`, `late_delivery_logistics` hoặc `null` | `get_shipment_summary` | finding |
| Payment/refund | facts, payments, payment/refund events | LLM, chỉ được trả duplicate, mismatch, split, refund pending/failed hoặc `null` | `get_payment_timeline`, `get_refund_timeline` | finding |
| Policy | policy version | Nạp rules theo issue | `get_policy` | `POLICY_LOADED` |
| Conflict resolver | history vs order vs shipment | Chọn incident window trước `opened_at`, ghi `data_conflicts` | Không gọi tool | trong `scoped_evidence` / `analyze_case` |
| Verifier | output ứng viên | Rule engine phản biện issue; hard checks schema/refs/tiền | Không gọi tool | `verification_completed` |

Mỗi specialist chỉ được báo issue thuộc domain của mình; issue ngoài domain bị bỏ thành
`null`. `facts` là các giá trị tính chính xác (ngày giao so với ngày dự kiến, lúc giao cho
carrier so với shipping limit, tổng capture, dòng payment trùng, trạng thái refund mới nhất),
lấy từ cùng các biến mà rule engine dùng, để LLM không phải tự so sánh ngày tháng hay số tiền.

Hai lớp chặn false positive trước verifier:
- **Grounding:** finding `late_delivery_seller`, `late_delivery_logistics` hoặc
  `payment_mismatch` mâu thuẫn với `facts` bị bỏ thành `null` (ghi `rejected_issue` trong
  handoff). Trên 100 dump, bộ lọc không loại nhầm case nào (28/28 case thật qua được).
- **allowed_issues:** coordinator chỉ được chọn issue mà ít nhất một specialist đã báo, hoặc
  `unsupported_claim` / `insufficient_evidence`; khi xem lại thì thêm issue của verifier.
  Chọn ngoài danh sách thì bị bỏ (`policy_decided.attributes.rejected_issue`).

Replay offline 100 case với cả hai lớp: 100/100 `PASS`, 0 lần LLM bất đồng với verifier
(trước đó có 11 case `PASS_RULES_OVER_LLM`).

Argument của mọi tool do code cố định: `case_id`, customer hint, `policy_version` và
order đã resolve. LLM không bao giờ nhìn thấy hoặc cung cấp `case_id` hay `evidence_ref`.

## 3. Entity resolution và A2A protocol

- Claimed order chỉ được chấp nhận khi có trong `get_customer_history`, còn ID dạng
  `candidate-*` bị loại. Không resolve được thì không gọi các tool theo order, và output sẽ là
  `insufficient_evidence`.
- Order có nhiều snapshot thì chọn dòng có purchase timestamp muộn nhất nhưng không sau
  `opened_at`, bỏ qua snapshot trùng với `get_order` (decoy). Cửa sổ incident kéo dài tới lần
  mua kế tiếp.
- Mọi message được correlation bằng `case_id`, handoff là `task_assigned` → `handoff`.
  Không có vòng lặp agent tự do: specialist gọi LLM 1 lần, coordinator tối đa 2 lần (thêm 1
  lần xem lại khi verifier phản đối).

## 4. Evidence và conflict lifecycle

`EvidenceGateway` validate mọi MCP envelope theo contract. `_Case` là ledger trong phạm vi
case: mỗi tool gọi tối đa 1 lần (cache cả kết quả lỗi). Gateway chỉ retry 1 lần khi tool
lỗi hoặc timeout, vì server từng lỗi chập chờn ở các call đầu của một lần chạy. Mỗi response
dùng được sẽ emit `tool_result_consumed` với `evidence_ref` nguyên bản do actor sở hữu tool
ghi. Output chỉ chứa refs có trong ledger (verifier kiểm tra `REFS`). Không chia sẻ evidence
giữa các case.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout/tool error | 1 (chờ 1.5s, tool chỉ đọc nên idempotent) | Ghi `None` trong ledger; specialist thấy trong `missing_tools` | `handoff/TOOL_CALL_FAILED` |
| Entity not found/ambiguous | 0 | Không gọi tool theo order; `insufficient_evidence` | `ENTITY_NOT_FOUND` |
| Source conflict | 0 | Chọn incident window theo `opened_at`; ghi `data_conflicts` | `verification_completed` |
| Invalid specialist result | 0 | Finding `None`; coordinator vẫn chạy | `handoff/NO_FINDING` |
| Finding mâu thuẫn facts | 0 | Bỏ thành `null` | `handoff.attributes.rejected_issue` |
| Coordinator chọn issue ngoài allowed | 0 | Bỏ; verifier yêu cầu xem lại | `policy_decided.attributes.rejected_issue` |
| LLM 429/5xx | 2 (backoff 1s, 2s) | Hết lượt: issue `insufficient_evidence` | `ISSUE_MISMATCH` |
| Lỗi bất ngờ trong case | 0 | Output rule-based rỗng evidence cho case đó | stderr `WARN` |

Budget MCP: 6 call/case (`history`, `order`, `items`, `shipment`, `payment_timeline`,
`policy`), thêm `refund_timeline` khi claim hoặc payment event có refund, và
`get_product_context` chỉ khi issue cuối là `unsupported_claim` (evidence bắt buộc). Hard cap
8 call. Không gọi `get_order_payments`, `get_sellers`. Đo offline trên 100 dump: bỏ bất kỳ
tool nào trong 6 tool cơ bản đều làm đổi một trường output (`affected_entities`,
`shipment_analysis`, `payment_analysis`, `data_conflicts`), nên đây là mức tối thiểu.
Mọi MCP call đều bị audit, nên chỉ chạy thật 1 lần cho mỗi bản nộp; tinh chỉnh prompt bằng
`scripts/replay_llm.py`.
Budget LLM: 4 call/case (5 khi verifier phản đối).

## 6. Verification invariants

Output validate đúng JSON Schema. Refs nằm trong tập đã consume. `evidence_refs` ở
top-level giữ đủ nhóm bắt buộc (history, order, items, shipment, payment, policy, cộng refund
hoặc product khi cần); `evidence_refs` của từng claim chỉ cite các domain quyết định claim
đó (`_CLAIM_EVIDENCE`), để tăng độ chính xác evidence. `no_action` không có refund.
Tổng refund lines bằng `recommended_refund_brl`. Action không trùng. Resolved và rejected
candidates không giao nhau. Confidence được hiệu chỉnh: LLM đồng ý với verifier thì
0.95–0.99 (cộng thêm theo số specialist cùng kết luận; calibration chấm
`(đúng - confidence)^2`); vẫn bất đồng thì lấy issue của
verifier với confidence 0.55 (`PASS_RULES_OVER_LLM`). CLI validate
schema lần nữa trước khi ghi file.

## 7. Reproducibility

- Python ≥ 3.11, dependency pin trong `uv.lock`.
- LLM: endpoint OpenAI-compatible (`OPENAI_BASE_URL`, `OPENAI_MODEL`), `temperature=0`,
  `response_format=json_object`. Model phải có **< 10 tỷ tham số**.
- Concurrency: `DAY09_CONCURRENCY` case song song (mặc định 4); trong một case, các MCP call
  và 3 specialist chạy song song.
- Lệnh: `day09 run`, `day09 validate`, `day09 package --output dist/submission.zip`.
  `DAY09_CASES=L3B_CASE_001,...` chạy một phần; `DAY09_RESUME=1` bỏ qua case đã finalize.
- Mỗi lần `run` lưu raw evidence vào `debug/` (gitignored, không đóng gói).
  `python scripts/replay_llm.py [case_id...]` chạy lại toàn bộ workflow LLM trên dump mà
  không gọi MCP, không ghi `outputs/` hay `traces/`; dùng để tinh chỉnh prompt.
- API key chỉ đọc từ `.env`.
