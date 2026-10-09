"""Runtime workshop settings; secrets never enter event context or records."""
import os
from pathlib import Path

from foundation_records import FoundationSettings, ProviderConfig
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


def values():
    try:result=assigned_values(Path(os.environ.get('BREADCAST_TEAM_CONFIG_DIR','/config')))
    except ProbeFailure:result={}
    for name in NAMES:
        if os.environ.get(name):result[name]=os.environ[name]
    return result


def settings():
    config=values()
    enabled=os.environ.get('BREADCAST_STACK_ENABLED','auto')
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
    return FoundationSettings(providers=providers,event={'event_id':'manual-event',
        'voice_id':config.get('ELEVENLABS_VOICE_ID') or config.get('BREADCAST_TTS_VOICE')},
        direction={'enabled':True},replay={'enabled':True,'index_version':'breadcast-caption-index-1',
            'embedding_version':config.get('COSMOS_EMBED1_MODEL','unresolved-embedding-model')})
