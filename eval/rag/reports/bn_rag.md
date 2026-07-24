RAG Chatbot E2E Report
======================
Run ID       : rag-20260618-153736
Dataset      : eval/rag/golden/golden_bn.json
K            : 5
Corpus       : published=34, embedded=34
Evaluated    : 20

RETRIEVAL
─────────────────────────────────────────────
Hit At K              :  0.632
Mrr                   :  0.458
Precision At K        :  0.126
Recall At K           :  0.632
Ndcg At K             :  0.501

END-TO-END
─────────────────────────────────────────────
Token F1 (avg)        :  0.567
Exact Match (avg)     :  0.000
Abstention Rate       :  1.000
False Refusal Rate    :  0.000
Citation Accuracy     :  0.632
Safety Pass Rate      :  0.000

PERFORMANCE
─────────────────────────────────────────────
P50 Latency (E2E)     : 3841ms
P90 Latency (E2E)     : 5330ms
P95 Latency (E2E)     : 6503ms
Cost (avg tokens)     : in=4581 out=178

PER-CATEGORY TOKEN F1
─────────────────────────────────────────────
chw_evaluation        : 0.567
