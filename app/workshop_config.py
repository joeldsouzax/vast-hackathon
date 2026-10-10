"""Runtime workshop settings; secrets never enter event context or records."""
import os
import re
from pathlib import Path

from foundation_records import FoundationSettings, Limits, ProviderConfig
from provider_probe import assigned_values, ProbeFailure


NAMES=('INGRESS_URL','USERNAME','PASSWORD','S3_CHUNKS_BUCKET','S3_SEGMENTS_BUCKET','S3_ENDPOINT',
    'BREADCAST_S3_ENDPOINT_URL','BREADCAST_S3_VERIFY',
    'ACCESS_KEY','SECRET_KEY','COSMOS3_REASON_URL','COSMOS3_REASON_MODEL',
    'YOLO_URL','COSMOS_EMBED1_URL','COSMOS_EMBED1_MODEL','GPU_BEARER_TOKEN',
    'WANDB_API_KEY','WANDB_TEAM','WANDB_PROJECT','BREADCAST_WANDB_BASE_URL',
    'BREADCAST_DIRECTOR_MODEL','BREADCAST_COMMENTATOR_MODEL','BREADCAST_SEGMENTOR_MODEL',
    'BREADCAST_TTS_URL','BREADCAST_TTS_MODEL','BREADCAST_TTS_API_KEY','BREADCAST_TTS_VOICE',
    'BREADCAST_TTS_PROTOCOL','BREADCAST_TTS_LANGUAGE')
NAMES+=('ELEVENLABS_API_KEY','ELEVENLABS_VOICE_ID','ELEVENLABS_MODEL_ID','BREADCAST_ELEVENLABS_BASE_URL')
NAMES+=('GEMINI_API_KEY','AI_STUDIO_KEY','BREADCAST_GEMINI_BASE_URL','BREADCAST_GEMINI_MODEL',
    'BREADCAST_GEMINI_EMBEDDING_MODEL','BREADCAST_GEMINI_SPEECH_MODEL','BREADCAST_GEMINI_DETECTION_MODEL','BREADCAST_GEMINI_VOICE',
    'SUPABASE_PROJECT_ID','SUPABASE_URL','SUPABASE_SECRET_KEY','SUPABASE_SERVICE_ROLE_KEY','BREADCAST_SUPABASE_BUCKET',
    'BREADCAST_RUNTIME_KEY','BREADCAST_PROVIDER_STACK')


def values():
    try:result=assigned_values(Path(os.environ.get('BREADCAST_TEAM_CONFIG_DIR','/config')))
    except ProbeFailure:result={}
    for name in NAMES:
        if os.environ.get(name):result[name]=os.environ[name]
    if not result.get('GEMINI_API_KEY') and result.get('AI_STUDIO_KEY'):
        result['GEMINI_API_KEY']=result['AI_STUDIO_KEY']
    if not result.get('SUPABASE_URL') and result.get('SUPABASE_PROJECT_ID'):
        project=result['SUPABASE_PROJECT_ID']
        if not re.fullmatch(r'[a-z]{20}',project):
            raise ValueError('SUPABASE_PROJECT_ID must be the 20-letter project reference, or set SUPABASE_URL')
        result['SUPABASE_URL']='https://'+project+'.supabase.co'
    return result


def settings():
    config=values()
    enabled=os.environ.get('BREADCAST_STACK_ENABLED','auto')
    if os.environ.get('BREADCAST_PROVIDER_STACK','gemini-supabase')=='gemini-supabase':
        if enabled=='0':return FoundationSettings()
        # Configure honest unavailable states even before cloud projects exist.
        embedding=config.get('BREADCAST_GEMINI_EMBEDDING_MODEL','gemini-embedding-2')
        providers={name:ProviderConfig(adapter='live',protocol='gemini-supabase-v1',version='unknown')
            for name in ('storage','jobs','cosmos','search','llm','speech')}
        providers['search']=providers['search'].model_copy(update={
            'version':'breadcast-gemini-caption-768-v1','model_id':embedding})
        providers['yolo']=ProviderConfig(adapter='live',protocol='gemini-supabase-v1',version='unknown')
        return FoundationSettings(providers=providers,
            event={'event_id':'manual-event','voice_id':config.get('BREADCAST_GEMINI_VOICE')},
            # One original budget covers analysis, a cited line, TTS, and delivery.
            # The former 8s window expired before speech could be prepared.
            limits=Limits(window_s=2.0,step_s=2.0,concurrency=4,live_deadline_s=20.0,call_timeout_s=10.0,retries=0),
            direction={'enabled':True,'role_timeout_s':12.0},
            replay={'enabled':True,'candidate_s':60.0,'preparation_s':45.0,
                'recall_s':45.0,'recall_expiry_s':60.0,'query_s':5.0,
                'index_version':'breadcast-gemini-caption-768-v1','embedding_version':embedding})
    if os.environ.get('BREADCAST_PROVIDER_STACK')!='workshop':
        raise ValueError('BREADCAST_PROVIDER_STACK must be gemini-supabase or workshop')
    if enabled=='0' or enabled=='auto' and not any(config.get(k) for k in ('INGRESS_URL','COSMOS3_REASON_URL','YOLO_URL','WANDB_API_KEY','ELEVENLABS_API_KEY')):
        return FoundationSettings()
    providers={name:ProviderConfig(adapter='live',version='unknown',protocol='workshop-v1',
        endpoint=config.get(endpoint),model_id=config.get(model) if model else None)
        for name,endpoint,model in (
            ('storage','INGRESS_URL',None),('jobs','INGRESS_URL',None),
            ('cosmos','COSMOS3_REASON_URL','COSMOS3_REASON_MODEL'),('yolo','YOLO_URL',None),
            ('search','COSMOS_EMBED1_URL','COSMOS_EMBED1_MODEL'))}
    # These are application index contract versions, not claims about a model's weights.
    providers['search']=providers['search'].model_copy(update={'version':'breadcast-caption-index-1',
        'model_id':config.get('COSMOS_EMBED1_MODEL','unresolved-embedding-model')})
    providers['llm']=ProviderConfig(adapter='live',protocol='workshop-v1',version='unknown',
        endpoint=config.get('BREADCAST_WANDB_BASE_URL','https://api.inference.wandb.ai/v1'),secret_env='WANDB_API_KEY')
    providers['speech']=ProviderConfig(adapter='live',protocol='workshop-v1',version='unknown',
        endpoint=config.get('BREADCAST_TTS_URL'),model_id=config.get('BREADCAST_TTS_MODEL'),secret_env='BREADCAST_TTS_API_KEY')
    # Workshop GPU/W&B latency is far above fixture budgets. Widen analyze,
    # role, and replay windows so live proof can finish on this VM.
    return FoundationSettings(providers=providers,event={'event_id':'manual-event',
        'voice_id':config.get('ELEVENLABS_VOICE_ID') or config.get('BREADCAST_TTS_VOICE')},
        limits=Limits(live_deadline_s=120.0,call_timeout_s=90.0),
        direction={'enabled':True,'role_timeout_s':60.0},
        replay={'enabled':True,'candidate_s':120.0,'preparation_s':90.0,
            'recall_s':90.0,'recall_expiry_s':120.0,'query_s':20.0,
            'index_version':'breadcast-caption-index-1',
            'embedding_version':config.get('COSMOS_EMBED1_MODEL','unresolved-embedding-model')})
