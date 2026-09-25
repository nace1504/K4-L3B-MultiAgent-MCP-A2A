# Day09 public contracts

Đây là nguồn chuẩn duy nhất cho các contract công khai của Day09 V2. Nội dung
trong thư mục này được phát hành kèm repo học viên và được dùng bởi API, MCP
gateway và submission validator.

Contract công khai gồm:

- registry của hai variant `l3a` và `l3b`;
- cấu trúc submission manifest;
- output schema của từng variant;
- MCP evidence envelope;
- observable trace event;
- public scoring policy, weights, hard gates and feedback visibility.

Contract không chứa case manifest chính thức, public/private membership,
constraint oracle, reference output, case-level private report hoặc generator seed.

Mỗi release phải giữ nguyên file đã phát hành. Khi cần thay đổi breaking,
tạo schema version mới thay vì sửa ý nghĩa của version cũ.
