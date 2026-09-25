# L3B Architecture Record

Tài liệu mô tả quyết định đã hiện thực trong `src/student_agent/` (`workflow.py`, `llm.py`,
`evidence.py`). Không ghi system prompt hay chain-of-thought.

## 1. System overview

```text
case input
   │
   ▼
Coordinator (code) ──task_assigned──▶ Entity Resolver (LLM: get_order, get_customer_history)
   │◀──────────────── handoff ─────────┘
   │  host: build_versions()  → chọn phiên bản đơn hàng theo opened_at (rule, không LLM)
   │
   ├─task_assigned─▶ Payment/Refund Agent (LLM) ─┐
   ├─task_assigned─▶ Policy Agent (LLM)          ├─ chạy song song, handoff về Coordinator
   └─task_assigned─▶ Shipment Agent (LLM)*       ┘   (*chỉ khi claim liên quan giao hàng/fulfilment)
   │  host: evidence floor + host_facts + detected_conflicts
   ▼
Conflict Resolver (LLM, không tool) ──handoff──▶ Verifier (code) ──(≤1 vòng sửa)──▶ output
                                                     │
MCP Gateway ◀── mọi tool call do HOST thực thi ──────┴── trace.jsonl (sự kiện quan sát được)
```

LLM chỉ chọn tool và suy luận trên evidence thật; **host code** thực thi MCP call, giữ
`evidence_ref`, tính số liệu và áp đặt các giá trị tất định từ policy.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer (`entity-resolver`, LLM) | candidate ids, claimed id, customer hint | Resolve candidate có `get_order` thật và thuộc history khách; reject phần còn lại | `get_order`, `get_customer_history` | status, resolved/rejected (host kiểm lại) → Coordinator |
| Coordinator (code) | case + kết quả entity | Tạo A2A envelope, định tuyến claim topic → specialist, gom kết quả | không | `task_assigned` / nhận `handoff` |
| Order/product | — | Gộp vào Payment (items để tính tổng đơn) và Policy (product context) | — | — |
| Shipment (`shipment-agent`, LLM) | order id, version context | Verdict giao hàng, late seller, timeline_complete | `get_shipment_summary`, `get_sellers` | verdict + citations → Coordinator |
| Payment/refund (`payment-agent`, LLM) | order id, version context | Verdict thanh toán/hoàn tiền, totals khớp `host_facts` | `get_payment_timeline`, `get_refund_timeline`, `get_order_items`, `get_order_payments` (chỉ khi timeline lỗi) | verdict + totals + citations → Coordinator |
| Policy (`policy-agent`, LLM) | policy_version, order id | Lấy policy đúng version + product context; không kết luận case | `get_policy`, `get_product_context` | policy context → Coordinator |
| Conflict resolver (LLM) | briefing: findings, host_facts, policy rules, detected_conflicts, evidence index | primary_issue, claim_assessments, data_conflicts, parties, actions, confidence | **không** | draft assessment → Verifier |
| Verifier (code) | draft + evidence store | Kiểm invariant (mục 6), yêu cầu sửa tối đa 1 vòng | không | `verification_completed` |

Least privilege được **ép ở host**: mỗi agent có allow-list riêng (`TOOL_PERMISSIONS`); tool
ngoài danh sách không được thực thi (`tool_not_permitted`). Thêm guard phạm vi: specialist chỉ
được hỏi đúng order đã resolve; `policy_version` phải trùng case; `customer_unique_id` phải
là hint của case; order id sai định dạng (vd `candidate-NNN`) bị từ chối **không gọi MCP**.

## 3. Entity resolution và A2A protocol

- Candidate = `claimed_order_id` ∪ `candidate_order_ids` (loại trùng). ID không đúng định dạng
  32-hex bị reject ngay, không tốn MCP call.
- LLM đề xuất resolved/rejected; host chỉ chấp nhận order có evidence `get_order` thành công
  trong case **và** có trong customer history (nếu history có dữ liệu). Mọi candidate còn lại
  vào `rejected_candidates`. `status`: 1 order → `resolved`, >1 → `ambiguous`, 0 → `not_found`.
  Nếu status LLM khác status host tính, confidence entity bị hạ ≤ 0.6.
- Không resolved → trả output `insufficient_evidence` / `needs_investigation`, không gọi specialist.
- **Envelope A2A** (`A2AMessage`): `{sender, recipient, case_id, correlation_id, task, payload}`.
  Coordinator tạo `correlation_id` cho mỗi task; reply dùng lại correlation_id. Trace chỉ ghi
  `actor`, `target`, `decision_code` (tên task/verdict), `evidence_refs` thật và
  `attributes.correlation_id` — **không** ghi payload/prompt/reasoning.
- Chống vòng lặp: mỗi agent tối đa 4 vòng tool (`MAX_ITERATIONS`), vòng cuối ép
  `tool_choice="none"` để phải trả lời; verifier chỉ cho 1 vòng sửa.

## 4. Evidence và conflict lifecycle

**Evidence grounding (evidence_store + local_id):**
1. LLM trả `tool_call` với tham số (không có `case_id`).
2. Host gọi `gateway.call(tool, case_id=<case hiện tại>, ...)`; gateway validate response theo
   `mcp-evidence-response-v1`.
3. `EvidenceStore` (1 instance/case, tạo mới mỗi case) lưu `{E1, E2, ...} → evidence_ref,
   domain, data`, cache theo `(tool, args)` nên cùng evidence không bị gọi lại, kể cả khi
   agent khác cần. Lần đầu mỗi actor dùng evidence → emit `tool_result_consumed` với
   `evidence_ref` thật.
4. LLM chỉ nhận `local_id` + view dữ liệu; mọi trích dẫn là `E#`. Host map `E#` →
   `evidence_ref` khi ráp output; `E#` không tồn tại bị loại và bị verifier báo lỗi.
   LLM không bao giờ thấy hay viết chuỗi `ev_...`.

**Phiên bản đơn hàng (quyết định nghiệp vụ quan trọng nhất):** dữ liệu MCP của một order có
thể chứa **hai phiên bản** chồng nhau (2 dòng history cùng order_id, item/payment trùng key,
capture ở hai mốc thời gian). Dòng `get_order` không phải lúc nào cũng là phiên bản của khiếu
nại. Host chọn phiên bản bằng rule trước khi đưa dữ liệu cho LLM:
- khiếu nại chỉ có thể nói về phiên bản đã tồn tại khi mở case → ưu tiên phiên bản có
  purchase **và** ngày giao dự kiến ≤ `opened_at` (đã đến hạn), kế đến phiên bản chỉ có
  purchase ≤ `opened_at`; chọn purchase mới nhất; không có `opened_at` hợp lệ → dùng dòng
  `get_order` (authoritative);
- mỗi bản ghi có ngày (item shipping limit, capture, refund, shipment event) thuộc phiên
  bản có purchase muộn nhất nhưng ≤ ngày bản ghi; payment row không có ngày được nối theo số
  tiền capture;
- LLM nhận `data` = phần của phiên bản đã chọn và `excluded_other_version_rows` riêng;
- hai phiên bản **trùng mốc thời gian** (không tách được) và evidence ủng hộ >1 cách hiểu →
  basis `inseparable_versions`: Conflict Resolver nhận danh sách
  `evidence_supported_interpretations`, claim topic chỉ được chọn nếu nằm trong danh sách đó,
  conflict ghi `selected_source: null`, confidence thấp.

**data_conflicts:** host phát hiện tất định (`detected_conflicts`): `order.version`
(get_order vs get_customer_history) và mỗi tool có dòng thuộc phiên bản khác
(`order_items`, `payment`, `refund`, `shipment.events`). Conflict Resolver quyết định
`selected_source` trong các nguồn đó; host giữ `field/sources` chuẩn, `resolution_code` là
căn cứ của host (vd `unique_due_version`, `rows_linked_to_selected_order_version`). Conflict
khác do LLM thêm chỉ được nhận khi sources là tên tool hợp lệ. Tối đa 5.

**Claim ↔ evidence:** claim topic là giả thuyết cần kiểm, không phải sự thật. Topic claim
`supported` khi trùng primary_issue; `requested_full_refund` là `supported` khi hoàn = tổng
đã capture, `partially_supported` khi 0 < hoàn < capture, `unsupported` khi hoàn = 0.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout / lỗi mạng | 1 retry, backoff 1s | coi như không có evidence, không phỏng đoán dữ liệu | không có `tool_result_consumed` |
| Mất kết nối MCP session (stream bị ngắt) | 3 lần/case, backoff 2s·n | CLI kết nối lại, xóa trace dở dang của case đó rồi chạy lại riêng case đó; case đã xong giữ nguyên | log `WARN` ra stderr (không vào trace) |
| MCP tool error (vd order không tồn tại, không có refund) | 0 (tất định) | cache kết quả rỗng trong case; LLM nhận `{"error": "no_result"}` | — |
| Entity not found/ambiguous | 0 | output `insufficient_evidence`, `needs_investigation` | `handoff entity_not_found/ambiguous`, `verification_completed passed_insufficient_evidence` |
| Source conflict | 0 | rule phiên bản + `data_conflicts`; không tách được → `selected_source: null` | `handoff inseparable_versions...` qua decision_code của resolver |
| Agent vượt vòng lặp / không trả JSON hợp lệ | 4 vòng tool/agent; OpenAI SDK retry 3 lần | answer = null → section dùng `insufficient_evidence` / host facts | `handoff <task>:no_answer` |
| Invalid specialist/resolver result | 1 vòng sửa | host áp giá trị tất định từ policy, confidence ≤ 0.5 | `verification_completed enforced_by_host[_after_repair]` |

**Efficiency:** cache `(tool,args)` trong case; ID sai định dạng không gọi MCP; `get_sellers`
chỉ khi seller có thể chịu trách nhiệm; `get_order_payments` chỉ khi timeline lỗi; Shipment
Agent chỉ được định tuyến cho topic giao hàng/fulfilment (payment + policy luôn chạy vì mọi
case có `requested_full_refund`). **Evidence floor**: nếu agent bỏ sót `get_payment_timeline`,
`get_order_items` hoặc `get_policy`, host tự gọi đúng 1 lần dưới quyền agent sở hữu tool đó.
Thực đo trên mẫu: 7–9 MCP call/case (gồm 1 call `get_refund_timeline` lỗi khi không có refund).

## 6. Verification invariants

Verifier (code) kiểm trước finalize; nếu có lỗi gửi danh sách lỗi cho Conflict Resolver sửa
tối đa 1 vòng:

- `primary_issue` thuộc enum **và** được evidence của phiên bản đã chọn ủng hộ (predicate
  tất định cho từng issue: trạng thái canceled/unavailable + đã capture, refund failed/pending,
  reconciliation mismatch mở, capture lặp không khớp tổng đơn, giao trễ + handoff trễ/đúng hạn,
  nhóm capture khớp đúng tổng đơn);
- `case_status`, `recommended_action`, số tiền hoàn theo rule policy của primary_issue; hoàn
  ≤ tổng capture; `no_action` → hoàn 0;
- seller party/late seller thuộc seller của order; không dùng seller id ví dụ trong policy;
- mọi local id được trích dẫn tồn tại trong evidence_store của case;
- claim_assessments phủ đúng tập claim của input;
- mọi conflict host phát hiện có trong `data_conflicts`;
- khi ráp output: schema l3b-output-v2 (CLI validate lại), `case_id` khớp, entity scope
  (affected ⊆ resolved, resolved ∩ rejected = ∅), totals lấy từ host facts, confidence ∈ [0,1].

Lỗi còn lại sau vòng sửa → host áp giá trị tất định (policy/host facts) và trace ghi
`enforced_by_host`.

**Confidence:** trần theo căn cứ chọn phiên bản (`single_version` 0.95, `unique_due_version`
0.93, `latest_due_version` 0.90, `latest_placed_version` 0.80, `authoritative_fallback`
0.70, `inseparable_versions` 0.65). Confidence của LLM chỉ được hạ trong một biên độ (trần −
0.08) khi verifier pass ngay; phải sửa → trần − 0.1 tối đa; còn lỗi → ≤ 0.5.

## 7. Reproducibility

- **Model:** `gpt-4o-mini` (biến `OPENAI_MODEL`, mặc định `gpt-4o-mini`), **temperature = 0**
  cho mọi lời gọi, Structured Outputs (`response_format` JSON Schema `strict: true`) cho mọi
  agent, function tools `strict: true`. Output LLM vẫn có thể dao động nhẹ giữa các lần chạy;
  các trường tất định (phiên bản, totals, policy fields) do host tính nên ổn định.
- **Grounding:** evidence_store + local_id (mục 4) — LLM không tạo `evidence_ref`.
- **Concurrency:** các case chạy tuần tự (CLI); trong 1 case, Payment/Policy/Shipment chạy
  song song bằng `asyncio.gather`, MCP call được tuần tự hóa qua lock của EvidenceStore.
  Timeout OpenAI 90s, SDK retry 3; MCP timeout 300s (starter).
- **Dependencies:** `pyproject.toml` (`mcp>=2,<3`, `openai>=1,<2`, `jsonschema`, `httpx2`,
  `python-dotenv`); Python ≥ 3.11. Không dùng random seed (event id của trace là ngẫu nhiên).
- **Chi phí đo trên mẫu:** ~7–13 lời gọi LLM/case, ~10–15k token input, ~1k token output.
- **Lệnh chạy:**

  ```bash
  python -m pip install -e ".[dev]"
  day09 validate-inputs
  day09 run
  day09 validate
  day09 package --output dist/submission.zip
  ```

- `.env` cần `COMPETITION_API_URL`, `COMPETITION_TEAM_API_KEY`, `MCP_ENDPOINT`,
  `OPENAI_API_KEY`, `OPENAI_MODEL`. Không key nào được ghi vào output/trace (package kiểm
  pattern `sk-team-`).
- Tương thích: `mcp_gateway.py` được sửa tối thiểu để đọc `is_error` (mcp 2.x) với fallback
  `isError`.
