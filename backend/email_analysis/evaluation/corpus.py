import hashlib
import json
from datetime import datetime
from pathlib import Path

from candidatures.models import Candidature
from ..schemas import Extraction

PATH = Path(__file__).with_name('corpus_v1.json')


def load_corpus():
    raw=PATH.read_bytes()
    corpus=json.loads(raw)
    if corpus.get('entirely_fictitious') is not True or len(corpus.get('cases',[])) != 40:
        raise ValueError('Invalid fictitious corpus.')
    ids={case['id'] for case in corpus['cases']}
    keys={item['key'] for item in corpus['candidate_fixtures']}
    if len(ids)!=40 or len(corpus['variability_cases'])!=5 or not set(corpus['variability_cases'])<=ids:
        raise ValueError('Invalid corpus identifiers.')
    for case in corpus['cases']:
        Extraction.model_validate(case['expected'] | {'justification':'Annotation fictive.'})
        when=datetime.fromisoformat(case['date_message'])
        if when.tzinfo is None or type(case['sent']) is not bool or not set(case['expected_association']['candidates'])<=keys:
            raise ValueError('Invalid annotation.')
    if {case['expected']['etat'] for case in corpus['cases']} - {None} != set(Candidature.Etat.values):
        raise ValueError('All eight states must be covered.')
    corpus['sha256']=hashlib.sha256(raw).hexdigest()
    return corpus
