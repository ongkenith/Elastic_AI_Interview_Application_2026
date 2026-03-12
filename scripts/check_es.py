from elasticsearch import Elasticsearch
from dotenv import load_dotenv
import os
load_dotenv()
es = Elasticsearch(os.getenv('ELASTICSEARCH_URL'), basic_auth=(os.getenv('ELASTICSEARCH_USER'), os.getenv('ELASTICSEARCH_PASSWORD')))

print('=== TRANSCRIPTS STORED IN ELASTICSEARCH ===')
r = es.search(index='transcript_index', query={'match_all':{}}, sort=[{'timestamp':{'order':'asc'}}], size=50)
total = r['hits']['total']['value']
print(f'Total transcript turns stored: {total}')
print()
for h in r['hits']['hits']:
    s = h['_source']
    sid = s.get('session_id','?')
    role = s.get('role','?').upper().ljust(10)
    content = s.get('content','')[:100]
    has_emb = 'embedding:YES' if s.get('content_embedding') else 'embedding:NO'
    print(f'  [{sid}] {role}  {has_emb}  {content}')

print()
print('=== SESSIONS ===')
r2 = es.search(index='interview_session_index', query={'match_all':{}}, size=10)
for h in r2['hits']['hits']:
    s = h['_source']
    print(f'  {s.get("session_id")}  status={s.get("status")}  stage={s.get("stage")}  started={s.get("started_at","-")}')
