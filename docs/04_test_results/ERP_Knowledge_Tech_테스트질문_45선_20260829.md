# SIMS AI `/knowledge-tech` ERP Knowledge 테스트 질문 45선

작성일: 2026-08-29

## 정식 보관 위치

이 질문집의 정식 프로젝트 보관 위치는 현재 파일이 있는
`docs/04_test_results`다.

성격:
- 개발/운영용 수동 UI Smoke 및 Retrieval 회귀 질문집
- ERP 내부 기술정보를 다루므로 `ERP_DB_INTERNAL` 취급
- Knowledge manifest에 자동 등록하지 않음
- 일반/public Knowledge corpus 또는 일반 사용자용 문서로 등록·배포하지 않음
- 향후 자동 Gate fixture로 전환할 때는 별도 검토 후 `tools` 쪽 테스트 데이터로 분리

현재 승인 대상 9건:
1. Table목록.docx
2. Rddbc010.txt
3. Rddbc040.txt
4. Rddbc110.txt
5. Rddbc120.txt
6. Rddbc130.txt
7. Rddbc140.txt
8. Rddbc210.txt
9. Rddbc220.txt

---

## 1. Table목록.docx — 5문항

1. `/knowledge-tech Table목록에서 Rddbc010은 어떤 용도의 테이블이야?`
2. `/knowledge-tech Table목록에서 Rddbc040은 어떤 테이블로 설명되어 있어?`
3. `/knowledge-tech Table목록에서 Rddbc110과 Rddbc120의 용도를 각각 알려줘.`
4. `/knowledge-tech Table목록에서 Rddbc130과 Rddbc140의 차이를 설명해줘.`
5. `/knowledge-tech Table목록에서 Rddbc210과 Rddbc220의 차이를 알려줘.`

권장 확인 포인트:
- Rddbc010=각종코드
- Rddbc040=제품
- Rddbc110=매입 / Rddbc120=출고
- Rddbc130=거래명세서 매입/매출 공통
- Rddbc140=세금계산서 매입/매출 공통
- Rddbc210=재고(실재고) / Rddbc220=재고(장부재고)

---

## 2. Rddbc010.txt — 5문항

6. `/knowledge-tech Rddbc010은 SIMS에서 어떤 정보를 관리하는 테이블이야?`
7. `/knowledge-tech Rddbc010의 PK는 어떤 필드들로 구성돼?`
8. `/knowledge-tech Rddbc010에서 Rd01_Gcode와 Rd01_Tcode의 역할 차이를 설명해줘.`
9. `/knowledge-tech Rddbc010에서 Rd01_Gcode가 9999인 경우 Rd01_Tcode는 무엇을 의미해?`
10. `/knowledge-tech 다른 테이블의 Gcode와 Tcode를 Rddbc010으로 해석하는 방법을 설명해줘.`

권장 확인 포인트:
- 모든 코드종류와 코드 관리
- PK = Rd01_Gcode + Rd01_Tcode
- 9999 그룹의 Tcode가 코드 종류 역할
- Gcode/Tcode 쌍의 코드명 해석 규칙

---

## 3. Rddbc040.txt — 5문항

11. `/knowledge-tech Rddbc040은 어떤 용도의 테이블이야?`
12. `/knowledge-tech Rddbc040의 제품 관련 주요 필드들을 알려줘.`
13. `/knowledge-tech Rddbc040에서 입고단가와 출고단가 관련 필드는 무엇이야?`
14. `/knowledge-tech Rddbc040에서 포장형태와 관련된 필드는 무엇이야?`
15. `/knowledge-tech Rddbc040에서 제품의 보관조건과 관련된 필드를 알려줘.`

권장 확인 포인트:
- 제품 테이블
- 문서에 실제 존재하는 Rd04_* 필드명과 설명을 근거로 답변하는지 확인
- 근거 없는 업무 의미를 모델이 임의 확장하지 않는지 확인

---

## 4. Rddbc110.txt — 5문항

16. `/knowledge-tech Rddbc110은 어떤 업무의 테이블이야?`
17. `/knowledge-tech Rddbc110의 PK는 어떤 필드들로 구성돼?`
18. `/knowledge-tech Rddbc110에서 입고일자, 거래처코드, 입고순번 필드는 무엇이야?`
19. `/knowledge-tech Rddbc110의 입출고구분 관련 필드는 무엇이야?`
20. `/knowledge-tech Rddbc110에서 수량과 할증수량 관련 필드를 알려줘.`

권장 확인 포인트:
- 매입/입고
- PK = Rd11_In_YyMmDd + Rd11_Ven_Cd + Rd11_In_Seq
- 입출고구분 = Rd11_Io_Gu_Gcode / Rd11_Io_Gu
- 수량 = Rd11_Quantity, 할증수량 = Rd11_Oquantity

---

## 5. Rddbc120.txt — 5문항

21. `/knowledge-tech Rddbc120은 어떤 업무의 테이블이야?`
22. `/knowledge-tech Rddbc120의 PK는 어떤 필드들로 구성돼?`
23. `/knowledge-tech Rddbc120에서 출고일자, 거래처코드, 출고순번 필드는 무엇이야?`
24. `/knowledge-tech Rddbc120의 출고 입출고구분 필드는 무엇인가?`
25. `/knowledge-tech Rddbc120에서 실제 영업사원과 관련된 필드는 무엇이야?`

권장 확인 포인트:
- 출고
- PK = Rd12_Out_YyMmDd + Rd12_Ven_Cd + Rd12_Out_Seq
- 입출고구분 = Rd12_Io_Gu_Gcode / Rd12_Io_Gu
- 현재 UI Smoke 기준 citation이 Rddbc120.txt로 연결되는지 확인

---

## 6. Rddbc130.txt — 5문항

26. `/knowledge-tech Rddbc130은 어떤 업무를 관리하는 테이블이야?`
27. `/knowledge-tech Rddbc130의 PK는 어떤 필드들로 구성돼?`
28. `/knowledge-tech Rddbc130에서 거래명세서 구분, 일자, 거래처코드, 순번 필드는 무엇이야?`
29. `/knowledge-tech Rddbc130에서 공급가액, 세액, 합계금액 관련 필드는 무엇이야?`
30. `/knowledge-tech Rddbc130에서 출고 거래명세서 구분 값은 무엇인가?`

권장 확인 포인트:
- 매입/매출 거래명세서 공통
- PK = Rd13_Trans_Di + Rd13_Trans_YyMmDd + Rd13_Ven_Cd + Rd13_Trans_Seq
- 금액 필드 = Rd13_Supply_Price / Rd13_Tax_Price / Rd13_Tot_Amt
- 출고 canonical 구분 값은 현재 승인/검증 계약 기준 `3`

---

## 7. Rddbc140.txt — 5문항

31. `/knowledge-tech Rddbc140은 어떤 업무를 관리하는 테이블이야?`
32. `/knowledge-tech Rddbc140의 PK는 어떤 필드들로 구성돼?`
33. `/knowledge-tech Rddbc140에서 세금계산서 구분, 일자, 거래처코드, 순번 필드는 무엇이야?`
34. `/knowledge-tech Rddbc140에서 공급가액과 세액 필드는 무엇이야?`
35. `/knowledge-tech Rddbc140에서 영수청구구분과 전자세금계산서 송부일자 관련 필드는 무엇이야?`

권장 확인 포인트:
- 세금계산서 매입/매출 공통
- PK = Rd14_Tax_Di + Rd14_Tax_YyMmDd + Rd14_Ven_Cd + Rd14_Tax_Seq
- 공급가액 = Rd14_Supply_Price
- 세액 = Rd14_Tax_Price
- 영수청구구분 = Rd14_Tax_Bill_Gcode / Rd14_Tax_Bill
- 전자세금계산서 송부일자 = Rd14_Report_Date

---

## 8. Rddbc210.txt — 5문항

36. `/knowledge-tech Rddbc210은 실재고와 장부재고 중 어느 쪽 월 집계 테이블이야?`
37. `/knowledge-tech Rddbc210의 PK는 어떤 필드들로 구성돼?`
38. `/knowledge-tech Rddbc210에서 제품코드, 재고위치코드, 재고월 필드는 무엇이야?`
39. `/knowledge-tech Rddbc210에서 입고수량과 출고수량 필드는 무엇이야?`
40. `/knowledge-tech Rddbc210에서 입출고구분과 재고위치 코드의 Gcode 필드는 무엇이야?`

권장 확인 포인트:
- 입/출고 월 실재고 집계
- PK = Rd21_Physic_Cd + Rd21_Stock_Cd + Rd21_Stock_YyMm + Rd21_Ven_Cd + Rd21_Stock_Apply_Cd + Rd21_Io_Gu
- 입고수량 = Rd21_In_Quantity
- 출고수량 = Rd21_Out_Quantity
- Gcode = Rd21_Stock_Cd_Gcode / Rd21_Io_Gu_Gcode

---

## 9. Rddbc220.txt — 5문항

41. `/knowledge-tech Rddbc220은 실재고와 장부재고 중 어느 쪽 월 집계 테이블이야?`
42. `/knowledge-tech Rddbc220의 PK는 어떤 필드들로 구성돼?`
43. `/knowledge-tech Rddbc220에서 제품코드, 재고위치코드, 재고월 필드는 무엇이야?`
44. `/knowledge-tech Rddbc220에서 입고수량과 출고수량 필드는 무엇이야?`
45. `/knowledge-tech Rddbc210과 Rddbc220의 필드 구조와 업무 목적 차이를 설명해줘.`

권장 확인 포인트:
- 입/출고 월 장부재고 집계
- PK = Rd22_Physic_Cd + Rd22_Stock_Cd + Rd22_Stock_YyMm + Rd22_Ven_Cd + Rd22_Stock_Apply_Cd + Rd22_Io_Gu
- 입고수량 = Rd22_In_Quantity
- 출고수량 = Rd22_Out_Quantity
- Rddbc210=실재고, Rddbc220=장부재고

---

## 운영 시 사용 방법

### 관리자/매니저 정상 Retrieval 확인
SYSTEM_ADMIN 또는 SSART_MANAGER 권한 계정으로 위 질문을 실행한다.

기대:
- 답변 생성
- citation 1건 이상
- 해당 승인 source_name 표시
- 근거 없는 필드/값을 추가하지 않음

### 일반 사용자 No-Leak 확인
SSART_STAFF 또는 ERP_DB_READ가 없는 계정에서는 대표 질문 1~2건만 실행한다.

기대:
- ERP 내부 내용 미노출
- source_name/source_key 미노출
- citation 0
- conflict notice로 문서 존재를 노출하지 않음

### 주의
이 질문집은 Retrieval/권한/근거 표시를 확인하기 위한 테스트 자료다.
실시간 DB 데이터 건수, 현재 매출/재고 수치 등은 `/knowledge-tech`가 아니라 SIMS/NLQ 조회 대상으로 구분한다.
