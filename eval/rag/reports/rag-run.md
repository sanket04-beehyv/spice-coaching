RAG Chatbot E2E Report
======================
Run ID       : rag-20260618-152343
Dataset      : eval/rag/golden/golden-dataset-rag.json
K            : 5
Corpus       : published=34, embedded=34
Evaluated    : 200

RETRIEVAL
─────────────────────────────────────────────
Hit At K              :  0.738
Mrr                   :  0.584
Precision At K        :  0.157
Recall At K           :  0.668
Ndcg At K             :  0.584

END-TO-END
─────────────────────────────────────────────
Token F1 (avg)        :  0.448
Exact Match (avg)     :  0.005
Abstention Rate       :  1.000
False Refusal Rate    :  1.000
Citation Accuracy     :  0.738
Safety Pass Rate      :  0.150

PERFORMANCE
─────────────────────────────────────────────
P50 Latency (E2E)     : 3687ms
P90 Latency (E2E)     : 7994ms
P95 Latency (E2E)     : 10861ms
Cost (avg tokens)     : in=5224 out=139

PER-CATEGORY TOKEN F1
─────────────────────────────────────────────
ambiguous             : 0.318
conversational_follow_up: 0.391
domain_specific_jargon: 0.454
edge_adversarial      : 0.446
factual_multi_hop     : 0.387
factual_simple        : 0.529
inferential           : 0.335
out_of_scope          : 0.574
