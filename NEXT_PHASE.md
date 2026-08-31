# Next Phase — Requirement-Driven Verification for AAOS (direction, not code)

Tài liệu định hướng cho phase sau. Đây là **hướng đi + scope**, chưa phải implementation.
Mục tiêu: từ PoC hiện tại (localize + patch bug theo framework AAOS) tiến tới
**kiểm tra code có đúng YÊU CẦU customer/OEM không** — và xa hơn, decompose +
truy vết requirement xuyên các tầng.

---

## 0. Bối cảnh: 3 lớp "đúng/sai", ta đang ở đâu

| Lớp | Nghĩa | Trạng thái |
|-----|-------|-----------|
| Syntax | Biên dịch được | Có parse-check (tree-sitter) trong loop; build thật để sau (GCP) |
| Semantic-framework | Đúng luật AAOS (tầng, AIDL, VHAL, VSS contract) | **ĐÃ CÓ** — PoC hiện tại |
| **Semantic-requirement** | Đúng yêu cầu customer (HMI/VSS spec/logic) | **PHASE SAU — tài liệu này** |

Nguyên tắc thứ tự: chỉ kiểm "đúng yêu cầu" khi code đã chắc syntax + framework.

---

## 1. Hai hướng của phase sau (liên quan nhưng khác scope)

### Hướng A — Requirement Conformance (code vs spec)
"Code này có thỏa spec OEM không?" So **code hiện có** với **spec customer** (VSS spec,
HMI design, logic diagram). Đây là mở rộng trực tiếp của bug-finding: thêm một loại bug
= "không khớp yêu cầu", bên cạnh "sai framework".

### Hướng B — Requirement Decomposition + Traceability (V-model)
Decompose `customer requirement → system requirement → SW requirement → HLD → LLD`,
giữ traceability xuyên suốt, verify ngược. Đây là **cả một hệ requirements-engineering**,
scope lớn hơn A nhiều.

**Khuyến nghị:** làm **A trước** (gần cái đã có, giá trị rõ), B là tầm nhìn xa hơn.

---

## 2. Bài học từ research (đã survey) — làm ĐÚNG, tránh ngu

Cả hai hướng đều có prior work, và đều có cùng một cái bẫy:

- **Near-exact cho A**: LLM static verification code vs NL requirements, domain
  vehicle-cybersecurity (arXiv 2605.17926) — thiết kế **2-agent**: `ruleMiner`
  (spec → rule kiểm chứng được) → `codeAuditor` (audit code theo rule). **Blueprint để bám.**
- **Near-work cho B**: HLR↔LLR decomposition/trace bằng LLM (2408.09127), requirement↔code
  traceability bằng RAG+LLM (Springer 2024), TraceLLM (2602.01253). Từng mắt xích đã có;
  **chuỗi 5 tầng liền mạch + domain AAOS thì chưa ai làm** → đây là góc đóng góp.
- **Cảnh báo lớn (2 paper)**: LLM **không tin cậy** khi phán code-vs-requirement; prompt
  càng chi tiết càng phán sai (**over-correction bias**). → KHÔNG để LLM so thẳng
  code-vs-spec một phát. Phải: spec → rule trung gian → audit → **verify bằng test/
  counterfactual**, không tin phán đoán trần.

**Định vị đóng góp:** kỹ thuật không mới (conformance/traceability đã có, kể cả trong
automotive); **novel ở áp dụng** — full-stack AAOS + spec OEM (VSS/HMI/logic) + multi-tenant
+ nối vào pipeline localize/patch sẵn có.

---

## 3. Hướng A khớp vào kiến trúc HIỆN TẠI ở đâu

Điểm mạnh: A **tái dùng gần hết** những gì đã xây, không phải làm lại.

| Cần cho A | Đã có sẵn? |
|-----------|-----------|
| Lưu spec OEM cô lập per-customer | **Có** — multi-tenant store (option B) |
| Chunk spec có cấu trúc (VSS spec) | **Có** — VSS signal-tree chunker |
| Retrieve spec + code liên quan | **Có** — hybrid RAG |
| Agent suy luận qua tool | **Có** — ReAct + LangGraph |
| Verify (không tin phán đoán) | **Một phần** — diff/parse-check; cần thêm rule-check |

Việc MỚI phải làm cho A:
1. **Nạp spec OEM vào knowledge layer** — VSS spec (bắt buộc/range/timing), HMI design,
   logic diagram. Vấn đề khó: HMI/diagram phải ở dạng **máy đọc được**, không phải ảnh.
2. **ruleMiner**: agent trích spec → tập rule kiểm chứng được (trigger/constraint/prohibited).
3. **codeAuditor**: agent so code (đã retrieve) với rule, xuất "khớp / vi phạm + bằng chứng".
4. **Verification filter**: kiểm phán đoán bằng test/counterfactual, giảm false-positive.

---

## 4. Nút chặn thật (giống phase trước)

- **Spec máy-đọc-được**: VSS spec thì ổn (yaml/json). Nhưng **HMI design + logic diagram
  thường là ảnh/PDF/Figma** → agent không so được. Cần quyết cách biểu diễn (DSL? structured
  export? OCR+parse?). Đây là nút chặn lớn nhất của A, giống "gold label" là nút của eval.
- **Gold label vẫn cần**: để đo A có đúng không (bug-theo-spec đã biết đáp án).
- **False-positive**: over-correction bias → phải có verification filter, không thì demo mất tin.

---

## 5. Đề xuất thứ tự cho phase sau (để plan)

1. **Chốt dạng spec máy-đọc-được** cho VSS trước (dễ nhất) — bỏ qua HMI/diagram giai đoạn đầu.
2. **ruleMiner + codeAuditor tối thiểu** cho VSS spec: "signal X phải update ≤ 100ms / range /
   bắt buộc" vs code — dùng lại RAG + agent sẵn có.
3. **Verification filter** chống false-positive.
4. **Eval nhỏ** cho conformance (vài spec-violation có nhãn) — đo trước khi mở rộng.
5. Chỉ khi A chạy ổn → cân nhắc HMI/logic-diagram (khó) và Hướng B (decompose 5 tầng).

**Nguyên tắc xuyên suốt:** chặt nhỏ, mỗi tầng một PoC, đo được rồi mới mở rộng. Đừng ôm
cả chuỗi requirement-engineering một lúc — nó là hệ lớn, không phải một feature.

---

## 6. Tóm một dòng

Phase sau = **thêm lớp "đúng yêu cầu OEM"** lên trên lớp "đúng framework" đã có, bắt đầu từ
**VSS spec conformance** (tái dùng multi-tenant store + VSS chunker + RAG + agent), theo mẫu
2-agent ruleMiner→codeAuditor, có verification filter chống over-correction. Traceability
5 tầng (customer→system→SW→HLD→LLD) là tầm nhìn xa hơn, làm sau khi conformance chạy được.
