"""Offline, deterministic metrics. No database or network access."""
import json
import unicodedata
from collections import Counter
from itertools import combinations
from ..name_matching import NAME_MATCHING_POLICY

FIELDS = ('employeur', 'poste', 'date_candidature', 'etat', 'confirmation_reception')
EDITABLE = ('action', *FIELDS)


def normalized(field, value):
    if field in ('employeur', 'poste') and isinstance(value, str):
        return ' '.join(unicodedata.normalize('NFC', value).split()).casefold()
    return value


def rate(correct, total):
    return {'correct':correct, 'total':total, 'rate':correct / total if total else None}


def summarize(corpus, observations):
    """Baseline only; repetitions are reserved for variability, never pooled."""
    baseline = {row['case_id']:row for row in observations if row['repetition']==1}
    rows = [(case, baseline.get(case['id'])) for case in corpus['cases']]
    attempted = [(case, row) for case, row in rows if row and row['status'] in ('completed','failed')]
    valid = [(case, row) for case, row in attempted if row['status']=='completed']
    classifications = {}
    for field in ('pertinent','action','etat'):
        confusion = Counter((str(case['expected'][field]),str(row['prediction'].get(field)) if row['status']=='completed' else '__error__') for case,row in attempted)
        correct = sum(case['expected'][field]==row['prediction'].get(field) for case,row in valid)
        classifications[field] = {**rate(correct,len(attempted)),
            'confusion':[{'expected':a,'predicted':b,'count':count} for (a,b),count in sorted(confusion.items())]}
    tp=sum(case['expected']['pertinent'] is True and row['prediction']['pertinent'] is True for case,row in valid)
    fp=sum(case['expected']['pertinent'] is False and row['prediction']['pertinent'] is True for case,row in valid)
    fn=sum(case['expected']['pertinent'] is True and row['prediction']['pertinent'] is False for case,row in valid)
    fn+=sum(case['expected']['pertinent'] is True and row['status']=='failed' for case,row in attempted)
    precision=tp/(tp+fp) if tp+fp else None
    recall=tp/(tp+fn) if tp+fn else None
    classifications['pertinent'].update(precision=precision,recall=recall,
        f1=2*precision*recall/(precision+recall) if precision is not None and recall is not None and precision+recall else None)
    fields = {}
    for field in FIELDS:
        correct=sum(normalized(field,case['expected'][field])==normalized(field,row['prediction'].get(field)) for case,row in valid)
        known=[(case,row) for case,row in valid if case['expected'][field] is not None]
        unknown=[(case,row) for case,row in valid if case['expected'][field] is None]
        fields[field]={'all_attempted':rate(correct,len(attempted)),
            'expected_non_null':rate(sum(normalized(field,case['expected'][field])==normalized(field,row['prediction'].get(field)) for case,row in known),len(known)),
            'unknown_preserved':rate(sum(row['prediction'].get(field) is None for _,row in unknown),len(unknown)),
            'invented_when_unknown':sum(row['prediction'].get(field) is not None for _,row in unknown)}
    differences=[]
    for case,row in valid:
        changed=[field for field in EDITABLE if normalized(field,case['expected'][field])!=normalized(field,row['prediction'].get(field))]
        differences.append({'case_id':case['id'],'fields':changed,'count':len(changed)})
    wanted_found=found=wanted=0
    for case,row in valid:
        gold=set(case['expected_association']['candidates']); actual=set(row['association']['candidates'])
        wanted_found+=len(gold&actual);found+=len(actual);wanted+=len(gold)
    association_precision=wanted_found/found if found else None
    association_recall=wanted_found/wanted if wanted else None
    return {'extraction_representation':{'source':'original validated model fields',
                'html_entities_decoded_for_extraction_scores':False,
                'text_comparison':'NFC, whitespace and case normalization only'},
        'association_input_normalization':NAME_MATCHING_POLICY,
        'schema_valid':rate(len(valid),len(attempted)),
        'missing_fields_annotation':rate(sum(set(case['expected']['informations_manquantes'])==set(row['prediction'].get('informations_manquantes',[])) for case,row in valid),len(attempted)),
        'errors':dict(Counter(row.get('error_code','unknown') for _,row in attempted if row['status']=='failed')),
        'coverage':{'corpus_size':len(rows),'attempted':len(attempted),'completed':len(valid),
                'failed':len(attempted)-len(valid),'pending':len(rows)-len(attempted)},
        'classification':classifications,'extraction':fields,
        'association':{'exact_set':rate(sum(set(case['expected_association']['candidates'])==set(row['association']['candidates']) for case,row in valid),len(attempted)),
            'precision_micro':association_precision,'recall_micro':association_recall,
            'f1_micro':2*association_precision*association_recall/(association_precision+association_recall) if association_precision is not None and association_recall is not None and association_precision+association_recall else None,
            'ambiguity':rate(sum(case['expected_association']['ambiguous']==row['association']['ambiguous'] for case,row in valid),len(attempted)),
            'level':rate(sum(case['expected_association']['level']==row['association']['level'] for case,row in valid),len(attempted)),
            'automatic_target_violations':sum(row['association']['target'] is not None for _,row in valid)},
        'corrections':{'interpretation':'Field differences from gold labels, not measured human time or observed user edits.',
            'proposals_with_field_differences':rate(sum(row['count']>0 for row in differences),len(valid)),
            'field_differences':sum(row['count'] for row in differences),'per_case':differences,
            'explicit_target_choices_required':sum(row['prediction']['action'] in ('changement_etat','confirmation_reception') for _,row in valid)}}


def variability(corpus, observations):
    result=[]
    for case_id in corpus['variability_cases']:
        rows=[row for row in observations if row['case_id']==case_id and row['status']=='completed']
        fingerprints=[json.dumps({field:normalized(field,row['prediction'].get(field)) for field in ('pertinent',*EDITABLE)},sort_keys=True) for row in rows]
        pairs=list(combinations(fingerprints,2))
        result.append({'case_id':case_id,'completed_repetitions':len(rows),'expected_repetitions':3,
            'distinct_outputs':len(set(fingerprints)), 'pairwise_agreement':rate(sum(a==b for a,b in pairs),len(pairs)),
            'per_field_agreement':{field:rate(sum(normalized(field,a['prediction'].get(field))==normalized(field,b['prediction'].get(field))
                for a,b in combinations(rows,2)),len(pairs)) for field in ('pertinent',*EDITABLE)}})
    return result
