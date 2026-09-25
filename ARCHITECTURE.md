# L3B Architecture Record

Tài liệu này mô tả các quyết định có thể kiểm chứng của workflow. Hệ thống không ghi
prompt bí mật hoặc chain-of-thought vào output hay trace.

## 1. System overview

```text
Input
  → Entity/customer agent
  → Coordinator
  → Order/product + shipment + payment/refund + policy specialists
  → Conflict resolver
  → Verifier
  → Output

MCP evidence ───────────────→ case-local evidence ledger ───────────────→ Trace
```

`solve_case` điều phối các vai trò bằng hàm bất đồng bộ. Hệ thống không phụ thuộc vào
framework multi-agent hoặc mô hình ngôn ngữ. Quyết định nghiệp vụ được tạo bằng quy tắc
xác định từ MCP evidence và policy.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer | case, candidates, customer hint | Chọn occurrence đúng theo customer scope và `opened_at`; reject candidate sai | `get_customer_history`, `get_order` | resolved order và customer context |
| Coordinator | case và handoff | Lập kế hoạch tool tối thiểu, phân công, gom kết quả | discovery; không tự tạo evidence | task assignment và final handoff |
| Order/item | resolved order | Lấy item và seller ID cần cho entity, payment và trách nhiệm | `get_order_items` | affected item/seller entities |
| Shipment | resolved order occurrence | Phân tích timeline và actor gây chậm | `get_shipment_summary` | shipment verdict, late seller IDs |
| Payment/refund | resolved order occurrence | Reconcile capture, duplicate, mismatch và refund lifecycle | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | totals và payment verdict |
| Policy | policy version và issue đã xác minh | Chọn case status, action, refund và responsible party | `get_policy` | policy decision |
| Conflict resolver | specialist results | Áp dụng customer/time scope khi snapshot mâu thuẫn | Không gọi tool mới | `data_conflicts` và normalized evidence |
| Verifier | candidate output | Kiểm tra invariant trước finalize | Không gọi MCP | verification result |

Tool discovery được cache trong một MCP session. Mỗi specialist chỉ được cấp các tool
thuộc domain của mình.

## 3. Entity resolution và A2A protocol

Entity resolver lọc customer history theo claimed/candidate order ID. Khi một ID xuất hiện
ở nhiều snapshot, occurrence có `order_purchase_timestamp` gần nhất nhưng không sau
`opened_at` được ưu tiên. Nếu không có occurrence quá khứ, resolver chọn occurrence gần
`opened_at` nhất; `get_order` là fallback và nguồn xác minh độc lập.

Handoff luôn correlation bằng `case_id`. Các decision code quan sát được gồm
`RESOLVE_CUSTOMER_AND_ORDER`, `ENTITY_RESOLVED`, `COLLECT_SCOPED_EVIDENCE`,
`EVIDENCE_COLLECTION_COMPLETE`, `CONFLICTS_RESOLVED` và `OUTPUT_INVARIANTS_PASSED`.
Workflow không có vòng lặp agent. Một tool lỗi được handoff về coordinator và không được
retry tự động, tránh nhân đôi side effect/audit call.

## 4. Evidence và conflict lifecycle

`EvidenceGateway` validate mọi MCP envelope bằng public contract. `EvidenceLedger` giữ
response và cache theo `(tool_name, arguments)` trong phạm vi một case. Evidence không
được chia sẻ giữa các case.

Mỗi response được sử dụng sẽ emit `tool_result_consumed` với nguyên bản `evidence_ref`.
Output chỉ dùng refs do MCP cấp. Claim assessment liên kết claim với refs của domain liên
quan; danh sách top-level giữ refs đã dùng để dựng entity, customer, shipment, payment,
policy và resolution.

Khi direct order/payment/shipment snapshot chứa nhiều occurrence, conflict resolver chọn
customer-scoped occurrence bằng thời gian. Conflict được ghi vào `data_conflicts` cùng
sources, selected source và resolution code; dữ liệu thiếu không được biến thành fact.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout/tool error | 0 tự động | Giữ lỗi trong case ledger; tiếp tục domain độc lập | `handoff/TOOL_CALL_FAILED` |
| Tool không được discovery | 0 | Không gọi tên tool đoán; degrade evidence | `handoff/TOOL_NOT_DISCOVERED` |
| Entity not found | 0 | Schema-valid `insufficient_evidence` assessment | `ENTITY_NOT_FOUND` |
| Source conflict | 0 | Customer scope + temporal selection; ghi conflict | `CONFLICTS_RESOLVED` |
| Invalid output invariant | 0 | Fail closed trước khi ghi output | Python `ValueError` |

Baseline mỗi case dùng bảy tool: customer, order, items, order payments, payment timeline,
shipment và policy. Product context không được gọi vì schema output không sử dụng product field.
Refund timeline chỉ được gọi cho `refund_pending` hoặc
`refund_failed`, vì gateway biểu diễn trường hợp không có refund bằng tool error. Seller
details chỉ được gọi cho seller-delay. Giới hạn đồng thời trong một case là bốn call; cache
ngăn call trùng.

## 6. Verification invariants

Trước finalize, verifier kiểm tra:

- resolved và rejected candidate không giao nhau;
- refund line total bằng `recommended_refund_brl`;
- `no_action` không đề xuất refund;
- late seller phải có trong affected seller entities;
- captured/refunded/refundable totals được lấy từ occurrence đã chọn;
- case, output và trace dùng cùng `case_id`;
- confidence nằm trong schema bounds;
- evidence refs do MCP cấp và được trace khi sử dụng;
- CLI tiếp tục validate toàn bộ JSON Schema trước khi ghi output.

## 7. Reproducibility và model constraint

- Python: 3.11 trở lên; dependency bounds nằm trong `pyproject.toml`.
- Concurrency: tối đa bốn MCP calls trong một case; các case chạy tuần tự.
- Randomness: không sử dụng random seed trong quyết định nghiệp vụ.
- Model: workflow hiện tại không gọi LLM, tương đương **0 model parameters**.
- Nếu sau này thêm model, model phải có **ít hơn 10.000.000.000 tham số**; model 10B
  chính xác hoặc lớn hơn không được phép. Quyết định tài chính vẫn phải qua verifier xác định.
- Lệnh chuẩn: `day09 run`, `day09 validate`, rồi
  `day09 package --output dist/submission.zip`.
- API key chỉ đọc từ `.env`; không ghi vào source, output, trace hoặc tài liệu.
