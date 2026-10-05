# Báo cáo Day 19 — Flat RAG vs GraphRAG

**Họ tên:** Lê Công Tâm  **MSSV:** 2A202602406  **Ngày:** 2026-10-05

> Kỳ vọng và thang điểm: `SUBMISSION.md`. Mọi số liệu phải khớp với `ket_qua_benchmark_kg.txt`. Bản thiết kế ontology nộp riêng ở `report/ONTOLOGY.md`.

---

## 1. Chi phí (10 điểm)

Dán 2 bảng `Indexing` và `Querying` từ `ket_qua_benchmark_kg.txt`:

```
Chat model: gemini:gemini-3.5-flash-lite | Embedding: gemini:gemini-embedding-001 | top_k=3 | chunk_size=800 | chunks=176 | KG: 206 nodes / 407 rels

== Indexing (one-off)
pipeline  calls    in_tok  out_tok       USD  seconds
flat        176         0        0   0.00000    123.3
graph       196     34619     5813   0.00579    214.6

== Querying (mean per question)
pipeline  recall  judge   in_tok  out_tok       USD  seconds
flat        0.51   1.33      696       76   0.00010     1.95
graph       1.00   2.00     5804      173   0.00065     3.56
```

| Chỉ số | Flat | Graph | Graph / Flat |
| --- | --- | --- | --- |
| Indexing USD | $0.00000 | $0.00579 | +$0.00579 (tăng một lần) |
| Indexing giây | 123.3s | 214.6s | ×1.74 |
| Mỗi câu: USD | $0.00010 | $0.00065 | ×6.50 |
| Mỗi câu: giây | 1.95s | 3.56s | ×1.83 |
| Mỗi câu: in_tok | 696 | 5804 | ×8.34 |

**Chi phí tăng thêm đến từ đâu?**
> Ở pha Indexing, chi phí GraphRAG tăng thêm $0.00579 và 91.3 giây chủ yếu do phải gọi LLM trích xuất thực thể/quan hệ có cấu trúc JSON từ 20 bài báo tin tức (văn bản luật được xử lý bằng Regex nên chi phí là 0 USD). 
> Ở pha Querying, chi phí mỗi câu của GraphRAG cao hơn 6.5 lần do prompt phải nạp thêm danh sách dữ kiện đồ thị mở rộng đa bước (khoản luật, hình phạt, tóm tắt vụ án), khiến số input tokens trung bình tăng gấp 8.34 lần (từ 696 lên 5804 tokens).

---

## 2. Từng câu hỏi (10 điểm)

| Câu | Loại | Flat recall / judge | Graph recall / judge | Thắng | Vì sao (1 câu) |
| --- | --- | --- | --- | --- | --- |
| **Q1** | single-hop-law | 1.00 / 2 | 1.00 / 2 | **Hòa** | Định nghĩa tiền chất nằm trọn vẹn trong một điều luật (Điều 2 Luật PCMT), vector search lấy đúng chunk nên cả hai pipeline đều trả lời xuất sắc. |
| **Q2** | single-hop-news | 1.00 / 2 | 1.00 / 2 | **Hòa** | Thông tin vụ án 36kg ma túy và mức án tử hình của Tuấn, Tâm nằm gọn trong một bài báo, Flat RAG tìm đủ ngữ cảnh cục bộ nên trả lời đúng. |
| **Q3** | cross-kb | 0.33 / 1 | 1.00 / 2 | **Graph** | Tên bị cáo và mức án ở tin tức còn điều luật/khung hình phạt ở luật, Flat RAG không có chunk nào chứa cả hai nên tuyên bố thiếu thông tin; GraphRAG đi qua cầu nối Crime để lấy trọn vẹn cả hai phía. |
| **Q4** | cross-kb | 0.33 / 1 | 1.00 / 2 | **Graph** | Flat RAG chỉ biết hành vi tổ chức sử dụng ma túy nhưng bỏ cuộc ở câu hỏi mức phạt tối đa; GraphRAG traversal qua quan hệ `HAS_MAX_PENALTY` lấy đúng mức tù 20 năm hoặc chung thân ở khoản 4 Điều 255. |
| **Q5** | cross-kb-multi-hop | 0.40 / 1 | 1.00 / 2 | **Graph** | Flat RAG biết tội vận chuyển và chất MDMA nhưng chịu không biết thuộc khoản nào; GraphRAG nối tang vật MDMA 9,6kg sang Điều 250 và trích xuất đúng khoản 4 (mức phạt tử hình). |
| **Q6** | aggregation | 0.00 / 1 | 1.00 / 2 | **Graph** | Flat RAG không gom đủ các bài báo phân tán và không gọi tên đầy đủ các thực thể (recall 0.00); GraphRAG truy vấn ngược từ node Substance "MDMA" gom chính xác cả 3 vụ án của Cái Quang Huy, Lê Minh Thành và Viện Pháp y. |

---

## 3. Phân tích lỗi (20 điểm)

### Lỗi E1: Cầu nối gãy (Vụ án không nối được sang luật)

- **Hiện tượng:** Một số vụ án trong KB tin tức được trích xuất thành node `Case` nhưng không hề có quan hệ `[:CHARGED_WITH]` nào trỏ tới node `Crime`, dẫn đến việc bị cô lập và không thể traversal sang KB Luật.
- **Bằng chứng:** Truy vấn Cypher tìm các Case không có `CHARGED_WITH`:

```cypher
MATCH (k:Case) WHERE NOT (k)-[:CHARGED_WITH]->() 
RETURN k.name AS name, k.doc_id AS doc_id;
```

```
╒═════════════════════════════════════════════════╤════════════════════════════╕
│name                                             │doc_id                      │
╞═════════════════════════════════════════════════╪════════════════════════════╡
│"Vụ tông cảnh sát giao thông tại An Giang"       │"news-100260926112415229"   │
├─────────────────────────────────────────────────┼────────────────────────────┤
│"Triệt phá chuyên án A3-626P"                    │"news-100261002184934505"   │
└─────────────────────────────────────────────────┴────────────────────────────┘
```

- **Nguyên nhân:** 
  - Nằm ở **bước trích xuất tin tức** và **bản chất nội dung bài báo**: 
    - Ở bài báo `news-100260926112415229`, nội dung bài viết phản ánh giai đoạn bắt giữ nóng khi đối tượng vận chuyển ma túy tông xe vào CSGT; cơ quan điều tra mới tạm giữ người về hành vi chống người thi hành công vụ và chưa có quyết định khởi tố chính thức với tội danh ma túy cụ thể theo BLHS.
    - Do đó, LLM không tìm thấy tội danh nào khớp trong `DANH SÁCH TỘI DANH` nên trả về `charges: []`.
- **Đề xuất sửa:** 
  - Bổ sung cơ chế fallback (suy luận ngầm) trong trích xuất: nếu bài báo mô tả hành vi mang/chở ma túy và có tang vật ma túy cụ thể nhưng chưa khởi tố, gán nhãn `charge_status: 'nghi vấn'` và tự động liên kết dự phòng sang node `Article` thông qua cầu nối phụ `Substance` (`k-[:INVOLVES]->s<-[:MENTIONS]-cl<-[:HAS_CLAUSE]-a`). Đánh đổi: có thể gây nhiễu nếu vụ việc sau đó bị đình chỉ điều tra.

---

### Lỗi E4: Phép đo sai (Recall và Judge mâu thuẫn)

- **Hiện tượng:** Ở câu hỏi **Q6** (`aggregation`) của pipeline **Flat RAG**, điểm `recall = 0.00` nhưng điểm `judge = 1` (mâu thuẫn giữa 2 phương thức đánh giá).
- **Bằng chứng:** Trích từ file `ket_qua_benchmark_kg.txt`:

```
--- Q6 [aggregation] flat recall=0.00 judge=1 3.45s
Dựa trên ngữ cảnh, cả 3 vụ việc đều có liên quan đến ma túy MDMA:

* Vụ việc [1]: Lực lượng chức năng phát hiện bên trong thùng hàng có các viên nén màu xanh là MDMA (khối lượng gần 4,3kg).
* Vụ việc [2]: Thành bị bắt quả tang khi mang 5 viên ma túy đến điểm hẹn để bán, kết luận giám định xác định đây là ma túy MDMA.
* Vụ việc [3]: Kết quả giám định xác định số viên nén hình tam giác màu hồng - xám trong kiện hàng là MDMA (khối lượng hơn 5,3kg).
```

- **Nguyên nhân:** 
  - Nằm ở **thiết kế hàm đo `keyword_recall`**: Phép đo recall sử dụng `keyword_recall(answer, must_include)` so khớp chuỗi con chính xác (exact substring). Danh sách `must_include` của Q6 quy định: `["Cái Quang Huy", "Lê Minh Thành", "Pháp y tâm thần"]`.
  - Flat RAG trích xuất các đoạn văn bị phân mảnh, nên trong câu trả lời đối tượng chỉ xuất hiện dưới dạng `"Thành"` (thiếu họ "Lê Minh"), hoặc mô tả bằng khối lượng `"4,3kg"`, `"5,3kg"` mà không nhắc tên `"Cái Quang Huy"`. Do đó hàm đo tính `recall = 0/3 = 0.00`.
  - Ngược lại, LLM Judge đọc hiểu ngữ nghĩa nhận thấy câu trả lời đã phát hiện đúng 3 vụ án ma túy MDMA nên cho điểm `1/2` (đúng một phần).
- **Đề xuất sửa:** 
  - Nâng cấp `must_include` hỗ trợ alias/regex (ví dụ: `["Cái Quang Huy|4,3kg", "Lê Minh Thành|Thành", "Pháp y tâm thần"]`). 
  - Hoặc chuẩn hóa câu trả lời qua một bước NER trước khi tính recall. Đánh đổi: tốn thêm chi phí và thời gian chạy benchmark.

---

### Lỗi E6: Thuộc tính thiếu (Quan hệ `INVOLVED_IN` có trường rỗng)

- **Hiện tượng:** Trên quan hệ `[:INVOLVED_IN]`, trường `sentence` (mức án) hoặc `charge` (tội danh) thường xuyên bị rỗng (`""`).
- **Bằng chứng:** Truy vấn Cypher kiểm tra thuộc tính rỗng trên cạnh:

```cypher
MATCH (p:Person)-[r:INVOLVED_IN]->(k:Case) 
WHERE r.charge = "" OR r.sentence = "" 
RETURN p.name AS person, r.role AS role, r.charge AS charge, r.sentence AS sentence, k.name AS case_name 
LIMIT 3;
```

```
╒═════════════════╤═════════════════╤══════════════════════════════════╤══════════╤══════════════════════════════════════════════════════════════════════════╕
│person           │role             │charge                            │sentence  │case_name                                                                 │
╞═════════════════╪═════════════════╪══════════════════════════════════╪══════════╪══════════════════════════════════════════════════════════════════════════╡
│"Cái Quang Huy"  │"bị cáo"         │"vận chuyển trái phép chất ma túy"│""        │"Vụ vận chuyển hơn 10kg ma túy từ Đức về Việt Nam qua sân bay Nội Bài"    │
├─────────────────┼─────────────────┼──────────────────────────────────┼──────────┼──────────────────────────────────────────────────────────────────────────┤
│"Nguyễn Hữu Đức" │"người liên quan"│""                                │""        │"Vụ vận chuyển hơn 10kg ma túy từ Đức về Việt Nam qua sân bay Nội Bài"    │
├─────────────────┼─────────────────┼──────────────────────────────────┼──────────┼──────────────────────────────────────────────────────────────────────────┤
│"Dương Minh Tuấn"│"nghi phạm"      │"tổ chức sử dụng trái phép ma túy"│""        │"Vụ bắt giữ giang hồ 'Hoàng Nato' và 126 người liên quan 8 đường dây..."  │
└─────────────────┴─────────────────┴──────────────────────────────────┴──────────┴──────────────────────────────────────────────────────────────────────────┘
```

- **Nguyên nhân:** 
  - Phản ánh đúng **giai đoạn tố tụng** của vụ việc trong tin tức: Với Cái Quang Huy (bài báo lúc Tòa đang xét xử / nghị án chưa tuyên) hoặc Dương Minh Tuấn (mới bị bắt quả tang), bài báo hoàn toàn chưa có mức án cụ thể. 
  - Với nhân vật Nguyễn Hữu Đức, người này là tài xế grab giao hàng vô can ("người liên quan"), cơ quan công an không khởi tố nên `charge = ""` và `sentence = ""` là hoàn toàn chính xác theo sự thật khách quan.
- **Đề xuất sửa:** 
  - Trong ontology, bổ sung thuộc tính `case_status: 'đang điều tra' | 'đang xét xử' | 'đã tuyên án'` để phân biệt rỗng hợp lệ (do vụ việc chưa xét xử) và rỗng do lỗi trích xuất.

---

## 4. Kết luận (5 điểm)

Từ dữ liệu thực nghiệm đo đạc ở Mục 1 và Mục 2, chúng ta rút ra kết luận:

1. **Khi nào Flat RAG là đủ?**
   - Khi hệ thống phục vụ các câu hỏi **single-hop cục bộ** (như Q1 và Q2), nơi câu trả lời nằm trọn vẹn trong một điều luật hoặc một bài báo duy nhất.
   - Ở các trường hợp này, Flat RAG đạt độ chính xác tương đương GraphRAG (cùng đạt Recall 1.00 và Judge 2/2), nhưng có lợi thế vượt trội: **tốc độ phản hồi nhanh gần gấp đôi** (1.95s so với 3.56s) và **chi phí token rẻ hơn tới 6.5 lần** ($0.00010 so với $0.00065), đồng thời tiết kiệm 100% chi phí xây dựng đồ thị ban đầu.

2. **Khi nào BẮT BUỘC phải dùng Knowledge Graph (GraphRAG)?**
   - Khi bài toán đòi hỏi trả lời các câu hỏi **cross-KB (liên kết nhiều nguồn)**, **multi-hop**, hoặc **aggregation (tổng hợp phân tán)** (như Q3, Q4, Q5, Q6). 
   - Với các câu hỏi này, Flat RAG thất bại nặng nề (Recall chỉ từ 0% đến 40%, Judge chỉ đạt 1/2) do các thông tin cấu thành câu trả lời nằm ở các tài liệu rời rạc mà vector search không thể gom đủ trong top-k chunk. 
   - Ngược lại, **GraphRAG đạt độ chính xác tuyệt đối 100% (Recall 1.00 và Judge 2.00 trên toàn bộ các câu)** nhờ khả năng kết nối chính xác từ vụ án sang điều luật qua node cầu nối `Crime` và định lượng tang vật qua `Substance`.

3. **Bài toán kinh tế:**
   - Chi phí dựng đồ thị Knowledge Graph ($0.00579 cho 176 chunks) là một khoản đầu tư một lần (one-off) rất nhỏ, nhưng mang lại bước nhảy vọt về chất lượng: biến hệ thống từ một chatbot "bó tay trước câu hỏi phức tạp" thành một trợ lý am hiểu sâu sắc, có khả năng tra cứu và trích dẫn chuẩn xác từng điều khoản luật hình sự.

---

## 5. Tự kiểm (5 điểm)

```
$ pytest tests/ -q
................................................                         [100%]
48 passed in 0.06s

$ python bench_kg.py --check
[OK] Dữ liệu: 18 điều luật, 20 bài báo
[OK] KG-1 link_entity
[OK] Neo4j kết nối được
[provider] chat = gemini:gemini-3.5-flash-lite | embedding = gemini:gemini-embedding-001
[OK] KG-2 build_graph: 148 node / 311 cạnh, đường xuyên 2 KB dài 2 cạnh
[OK] KG-3 context: 23 dữ kiện, có Điều 251
[OK] KG-4 GraphRAGAgent.answer
[OK] Chi phí check: 1 lần gọi LLM, $0.00055. Graph nhỏ (luật + 1 bài) vẫn còn trong Neo4j để bạn xem; chạy --judge để dựng graph đầy đủ.
```

Ảnh Neo4j: `report/img/kg_count.png`, `report/img/kg_cross_kb.png`, `report/img/kg_my_case.png`.
Người đã chọn cho `kg_my_case.png`: **Cái Quang Huy** (vụ án vận chuyển ma túy qua sân bay Nội Bài, nối xuyên sang Điều 250 BLHS).

---

## Vấn đề gặp phải (không tính điểm)

- **Vấn đề Rate Limit (429 RESOURCE_EXHAUSTED) của Gemini API:**
  - *Hiện tượng:* Khi chạy `python bench_kg.py --judge` với 20 bài báo dồn dập, Gemini API free tier trả về lỗi `openai.RateLimitError: 429`.
  - *Giải pháp đã thực hiện:* Bổ sung cơ chế tự động thử lại với lũy thừa thời gian chờ (exponential backoff retry) trong `src/llm.py` và thêm khoảng giãn cách 1.5 giây giữa các bài báo trong `build_graph`. Quá trình indexing sau đó chạy trơn tru đến khi hoàn tất 100%.
