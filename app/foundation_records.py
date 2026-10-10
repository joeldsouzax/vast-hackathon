"""Event foundation contracts. JSON Schema is generated from these strict models."""
from __future__ import annotations

from fractions import Fraction
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ID = Annotated[str, Field(min_length=1, max_length=192, pattern=r'^[A-Za-z0-9_./:-]+$')]
Positive = Annotated[int, Field(gt=0)]
Nonnegative = Annotated[int, Field(ge=0)]


class Record(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True, allow_inf_nan=False, revalidate_instances='always')


class Interval(Record):
    start: int
    end: int

    @model_validator(mode='after')
    def ordered(self):
        if self.end <= self.start:
            raise ValueError('Intervals are half-open and must be nonempty')
        return self


class EventContext(Record):
    schema_version: Literal['1.0'] = '1.0'
    event_id: ID
    revision: Positive = 1
    title: str | None = Field(default=None, max_length=256)
    profile: Literal['soccer', 'stage', 'community'] = 'community'
    participants: list[str] = Field(default_factory=list, max_length=128)
    branding: dict[str, str] = Field(default_factory=dict)
    editorial_policy: dict[str, str] = Field(default_factory=dict)
    audio_policy: dict[str, str] = Field(default_factory=dict)
    commentary_style: str = Field(default='funny, witty live play-by-play with occasional brief analysis grounded in visible action; upbeat conversational delivery, playful warmth, quick punchlines, varied evidence-backed callbacks, natural emphasis and short pauses; leave room for event sound',min_length=1,max_length=2048)
    language: str = Field(default='en',min_length=1,max_length=32)
    pronunciations: dict[str, str] = Field(default_factory=dict)
    voice_id: str | None = Field(default=None,max_length=192)
    broadcast_delay_s: Annotated[float, Field(ge=0, le=10)] | None = None


class OfficialFact(Record):
    event_id: ID
    revision: Positive
    name: ID
    value: str | int | bool | None
    authority: Literal['human'] = 'human'
    effective_event_ms: int | None = None
    operation_key: ID


class SourceEpoch(Record):
    event_id: ID
    run_id: ID
    source_id: ID
    epoch: Positive
    slot: Annotated[int, Field(ge=1, le=5)]
    time_base: str
    discontinuity: str | None = None

    @model_validator(mode='after')
    def clock(self):
        try:time_base=Fraction(self.time_base)
        except (ValueError,ZeroDivisionError) as error:raise ValueError('Invalid native time base') from error
        if time_base <= 0:
            raise ValueError('Time base must be positive')
        return self


class Geometry(Record):
    native_width: Positive
    native_height: Positive
    rotation: Literal[0, 90, 180, 270] = 0
    output_width: Positive
    output_height: Positive
    scaled_width: Positive
    scaled_height: Positive
    pad_x: Nonnegative = 0
    pad_y: Nonnegative = 0

    @model_validator(mode='after')
    def fit(self):
        if self.pad_x + self.scaled_width > self.output_width or self.pad_y + self.scaled_height > self.output_height:
            raise ValueError('Scaled image exceeds output bounds')
        return self

    def native_point(self, x: float, y: float) -> tuple[float, float]:
        px, py = x * self.output_width - self.pad_x, y * self.output_height - self.pad_y
        if not 0 <= px <= self.scaled_width or not 0 <= py <= self.scaled_height:
            raise ValueError('Point is in padding')
        u, v = px / self.scaled_width, py / self.scaled_height
        if self.rotation == 90: u, v = v, 1-u
        elif self.rotation == 180: u, v = 1-u, 1-v
        elif self.rotation == 270: u, v = 1-v, u
        return u * self.native_width, v * self.native_height


class TimeMapping(Record):
    source: SourceEpoch
    revision: Positive
    valid: Interval
    origin_pts: int
    offset_event_ms: int
    rate_correction: Annotated[float, Field(gt=0)] = 1.0
    uncertainty_ms: Annotated[float, Field(ge=0)]
    calibration_evidence: list[ID] = Field(min_length=1)

    def event_ms(self, pts: int) -> float:
        if not self.valid.start <= pts < self.valid.end:
            raise ValueError('Mapping is outside its calibrated interval')
        return self.offset_event_ms + (pts-self.origin_pts)*float(Fraction(self.source.time_base))*1000*self.rate_correction


class MediaObject(Record):
    key: Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
    sha256: Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
    size: Positive
    format: str
    decoded_frames: Positive


class ChunkManifest(Record):
    schema_version: Literal['1.0'] = '1.0'
    chunk_id: ID
    source: SourceEpoch
    sequence: Nonnegative
    native: Interval
    file_native: Interval | None = None
    timeline_offset_pts: int = 0
    media: MediaObject
    geometry: Geometry
    event_mapping: TimeMapping | None = None
    last_receipt_utc: float | None
    notification_utc: float | None = None
    receipt_basis: Literal['unknown','frame-receipt','recording-notification'] = 'unknown'
    receipt_uncertainty_ms: Annotated[float, Field(ge=0)] = 0.0
    finalized_utc: float
    ready: Literal[True] = True
    configuration_revision: Positive
    provenance: Literal['camera', 'sample', 'server_video']

    @model_validator(mode='after')
    def mapping_owner(self):
        if self.receipt_basis=='frame-receipt' and self.last_receipt_utc is None:
            raise ValueError('Verified frame receipt is missing')
        if self.receipt_basis=='recording-notification' and self.notification_utc is None:
            raise ValueError('Recording notification time is missing')
        if self.file_native and (self.native.start!=self.file_native.start+self.timeline_offset_pts or
                                 self.native.end!=self.file_native.end+self.timeline_offset_pts):
            raise ValueError('Stored file and epoch timeline transform differ')
        m = self.event_mapping
        if m and (m.source != self.source or self.native.start < m.valid.start or self.native.end > m.valid.end):
            raise ValueError('Mapping does not cover this source interval')
        return self


class SourceHealth(Record):
    source_id: ID
    slot: Annotated[int, Field(ge=1, le=5)]
    epoch: Nonnegative
    state: Literal['RESERVED', 'ACTIVE', 'RECONNECTING', 'REVOKING']
    last_frame_age_s: Annotated[float, Field(ge=0)] | None
    buffer_seconds: Annotated[float, Field(ge=0)] | None
    buffer_ready: bool | None
    has_audio: bool | None


class ProgramState(Record):
    requested: Literal['HOLDING', 'LIVE', 'REPLAY']
    actual: Literal['HOLDING', 'LIVE', 'REPLAY']
    actual_target: dict = Field(max_length=16)
    applied_revision: Nonnegative
    primary_slot: Annotated[int, Field(ge=1, le=5)]
    primary_source_path: ID | None
    audio_slot: Annotated[int, Field(ge=1, le=5)]
    audio_source_path: ID | None
    audio_muted: bool
    replay_id: ID | None


class RuntimeState(Record):
    sampled_utc: Annotated[float, Field(gt=0)]
    program: ProgramState
    source_health: list[SourceHealth] = Field(max_length=5)
    crew_paused: bool
    policy: dict[str, int | float | bool] = Field(max_length=16)


class DecisionSnapshot(Record):
    event_id: ID
    run_id: ID
    context_revision: Positive
    configuration_revision: Positive
    evidence_revision: Nonnegative
    program_revision: Nonnegative
    control_revision: Nonnegative
    sources: list[SourceEpoch] = Field(default_factory=list, max_length=5)
    mapping_revisions: dict[str, int] = Field(default_factory=dict)
    decoder_revisions: dict[str, int] = Field(default_factory=dict,exclude_if=lambda value:not value)
    runtime: RuntimeState | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode='after')
    def runtime_revision(self):
        if self.runtime and self.runtime.program.applied_revision > self.program_revision:
            raise ValueError('Applied program state exceeds reviewed program revision')
        return self


class AnalysisWindow(Record):
    schema_version: Literal['1.0'] = '1.0'
    job_key: ID
    source: SourceEpoch
    native: Interval
    chunk_ids: list[ID] = Field(min_length=1, max_length=16)
    snapshot: DecisionSnapshot
    deadline_utc: float
    deadline_basis: Literal['unknown','frame-receipt','recording-notification'] = 'unknown'
    created_utc: float
    trace_id: ID

    @model_validator(mode='after')
    def owner(self):
        if (self.snapshot.event_id, self.snapshot.run_id) != (self.source.event_id, self.source.run_id):
            raise ValueError('Snapshot and source ownership differ')
        return self


class Detection(Record):
    pts: int
    label: str = Field(min_length=1, max_length=128)
    confidence: Annotated[float, Field(ge=0, le=1)]
    box: list[Annotated[float, Field(ge=0, le=1)]] = Field(min_length=4, max_length=4)
    track_id: str | None = None

    @model_validator(mode='after')
    def box_order(self):
        if self.box[2] <= self.box[0] or self.box[3] <= self.box[1]:
            raise ValueError('Detection box is empty')
        return self


class DetectedObject(Record):
    label: str = Field(min_length=1, max_length=128)
    box: list[Annotated[float, Field(ge=0, le=1)]] = Field(min_length=4, max_length=4)

    @model_validator(mode='after')
    def box_order(self):
        if self.box[2] <= self.box[0] or self.box[3] <= self.box[1]:
            raise ValueError('Object box is empty')
        return self


class ObjectFrame(Record):
    image_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    image_width: Positive
    image_height: Positive
    coordinate_space: Literal['inspected_frame'] = 'inspected_frame'
    objects: list[DetectedObject] = Field(max_length=24)


class ViewAssessment(Record):
    native: Interval
    subject_visible: bool
    quality: Literal['usable', 'obscured', 'blurred', 'motion', 'missing']
    adds: str = Field(min_length=1, max_length=256)


class Observation(Record):
    evidence_id: ID
    job_key: ID
    source: SourceEpoch
    native: Interval
    chunk_ids: list[ID] = Field(min_length=1, max_length=16)
    snapshot: DecisionSnapshot
    configuration_revision: Positive
    model_id: str = Field(min_length=1, max_length=192)
    model_version: str = Field(min_length=1, max_length=128)
    origin: Literal['fixture', 'provider']
    description: str = Field(min_length=1, max_length=2048)
    kind: Literal['observed', 'inferred']
    uncertainty: Annotated[float, Field(ge=0, le=1)]
    association_key: ID | None = None
    detections: list[Detection] = Field(default_factory=list, max_length=256)
    object_frame: ObjectFrame | None = Field(default=None, exclude_if=lambda value: value is None)
    produced_utc: float
    view: ViewAssessment | None = None
    replay_opportunity: Literal['quiet', 'stoppage', 'recap'] | None = None
    urgent_live: bool = False


class SceneEvent(Record):
    scene_id: ID
    revision: Positive
    source: SourceEpoch
    native: Interval
    evidence_ids: list[ID] = Field(min_length=1, max_length=128)
    description: str
    status: Literal['candidate', 'retracted'] = 'candidate'
    uncertainty: Annotated[float, Field(ge=0, le=1)]
    origin: Literal['fixture', 'provider']


class SearchQuery(Record):
    event_id: ID
    run_id: ID
    text: str = Field(min_length=1, max_length=1024)
    eligible: Interval | None = None
    limit: Annotated[int, Field(ge=1, le=10)] = 5
    index_version: str = 'fixture-index-1'
    embedding_version: str = 'fixture-labels-1'


class ProgramText(Record):
    cue_id: ID
    event_id: ID
    run_id: ID
    text: str = Field(max_length=2048)
    state: Literal['prepared', 'pending', 'started', 'completed', 'interrupted', 'canceled', 'expired', 'aired']
    event_ms: int | None
    program_revision: Nonnegative
    evidence_ids: list[ID] = Field(default_factory=list)
    basis: Literal['action', 'event_context'] = 'action'
    context_revision: Positive | None = None
    origin: Literal['fixture', 'controller']
    channel: Literal['speech', 'caption', 'intent', 'legacy'] = 'legacy'
    session_id: ID | None = None
    first_program_ms: Nonnegative | None = None
    last_program_ms: Nonnegative | None = None
    first_sample: Nonnegative | None = None
    last_sample: Nonnegative | None = None
    reason: str | None = Field(default=None, max_length=256)


class LLMResult(Record):
    text: str = Field(min_length=1, max_length=2048)
    snapshot: DecisionSnapshot
    origin: Literal['fixture', 'provider']
    model_id: str
    model_version: str


class SpeechResult(Record):
    media: MediaObject
    duration_s: Annotated[float, Field(gt=0)]
    sample_rate: Positive
    channels: Positive
    origin: Literal['fixture', 'provider']
    model_id: str
    voice_id: str | None = None
    transcript: str | None = Field(default=None, max_length=2048)
    configuration_revision: Positive = 1


class ProviderConfig(Record):
    adapter: Literal['disabled', 'fixture', 'live'] = 'disabled'
    endpoint: str | None = Field(default=None, exclude=True)
    secret_env: str | None = None
    model_id: str | None = None
    version: str | None = None
    protocol: str | None = None
    formats: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    storage_location: str | None = None
    trigger_filter: str | None = None
    worker_resources: dict[str, str] = Field(default_factory=dict)


class Limits(Record):
    window_s: Annotated[float, Field(gt=0, le=12)] = 6.0
    step_s: Annotated[float, Field(gt=0, le=12)] = 2.0
    concurrency: Annotated[int, Field(ge=1, le=4)] = 2
    call_timeout_s: Annotated[float, Field(gt=0)] = 10.0
    live_deadline_s: Annotated[float, Field(gt=0)] = 8.0
    retries: Annotated[int, Field(ge=0, le=2)] = 2
    pending_chunks: Positive = 256
    pending_bytes: Positive = 1073741824
    archive_seconds: Positive = 1800
    archive_bytes: Positive = 4294967296
    disk_reserve_bytes: Nonnegative = 2147483648
    context_seconds: Positive = 60
    context_records: Positive = 32
    context_utterances: Positive = 20
    context_hits: Positive = 5
    context_bytes: Positive = 65536
    pin_seconds: Positive = 60
    ledger_records: Positive = 20000

    @model_validator(mode='after')
    def windows(self):
        if self.step_s>self.window_s:raise ValueError('Window step exceeds analysis width')
        return self


class DirectionSettings(Record):
    enabled: bool = False
    # Workshop W&B round-trips often exceed the old 8s fixture cap.
    role_timeout_s: Annotated[float, Field(gt=0, le=120)] = 8.0
    attempts: Annotated[int, Field(ge=1, le=2)] = 2
    speech_max_s: Annotated[float, Field(gt=0, le=8)] = 8.0
    speech_asset_bytes: Annotated[int, Field(gt=0, le=16777216)] = 16777216
    speech_total_bytes: Annotated[int, Field(gt=0, le=33554432)] = 33554432
    action_records: Annotated[int, Field(ge=32, le=20000)] = 20000
    ambient_gain: Annotated[float, Field(ge=0, le=0.8)] = 0.7
    narrator_gain: Annotated[float, Field(gt=0, le=0.8)] = 0.7
    duck_gain: Annotated[float, Field(ge=0, le=1)] = 0.25
    ramp_ms: Annotated[int, Field(ge=10, le=200)] = 40
    max_magnification: Annotated[float, Field(ge=1, le=2)] = 2.0


class ReplaySettings(Record):
    enabled: bool = False
    # Workshop segmentor vision calls need more than the old 15/30s fixture caps.
    candidate_s: Annotated[float, Field(gt=0, le=180)] = 30.0
    preparation_s: Annotated[float, Field(gt=0, le=120)] = 15.0
    recall_s: Annotated[float, Field(gt=0, le=120)] = 45.0
    recall_expiry_s: Annotated[float, Field(gt=0, le=180)] = 60.0
    query_s: Annotated[float, Field(gt=0, le=30)] = 5.0
    query_records: Annotated[int, Field(ge=1, le=100)] = 100
    jobs: Annotated[int, Field(ge=1, le=20)] = 20
    attempts: Annotated[int, Field(ge=1, le=2)] = 2
    max_shots: Annotated[int, Field(ge=1, le=12)] = 6
    max_duration_s: Annotated[float, Field(gt=0, le=12)] = 12.0
    alignment_ms: Annotated[float, Field(gt=0, le=150)] = 150.0
    input_chunks: Annotated[int, Field(ge=1, le=32)] = 32
    input_bytes: Annotated[int, Field(gt=0, le=268435456)] = 268435456
    memory_bytes: Annotated[int, Field(gt=0, le=268435456)] = 268435456
    scratch_bytes: Annotated[int, Field(gt=0, le=268435456)] = 268435456
    ready_assets: Annotated[int, Field(ge=1, le=2)] = 2
    ready_bytes: Annotated[int, Field(gt=0, le=67108864)] = 67108864
    index_version: str = Field(default='fixture-index-1', min_length=1, max_length=128)
    embedding_version: str = Field(default='fixture-labels-1', min_length=1, max_length=128)

    @model_validator(mode='after')
    def budgets(self):
        if self.recall_s > self.recall_expiry_s or self.preparation_s > self.candidate_s:
            raise ValueError('Preparation must fit its original eligibility deadline')
        return self


class CropRect(Record):
    x: Annotated[float, Field(ge=0, le=1)]
    y: Annotated[float, Field(ge=0, le=1)]
    width: Annotated[float, Field(gt=0, le=1)]
    height: Annotated[float, Field(gt=0, le=1)]

    @model_validator(mode='after')
    def bounds(self):
        if self.x+self.width>1.000000001 or self.y+self.height>1.000000001:
            raise ValueError('Crop exceeds oriented source bounds')
        return self


class Abstention(Record):
    op: Literal['abstain']
    reason: str = Field(min_length=1, max_length=256)


class CameraIntent(Record):
    op: Literal['live', 'audio']
    slot: Annotated[int, Field(ge=1, le=5)]
    independent: bool = False
    muted: bool = False
    evidence_ids: list[ID] = Field(default_factory=list, max_length=32)
    reason: str = Field(min_length=1, max_length=256)


class FramingIntent(Record):
    op: Literal['crop', 'reset_crop']
    rect: CropRect | None = None
    evidence_ids: list[ID] = Field(default_factory=list, max_length=32)
    reason: str = Field(min_length=1, max_length=256)


class GraphicIntent(Record):
    op: Literal['graphics']
    preset: ID
    title: str | None = Field(default=None, min_length=1, max_length=80)
    subtitle: str | None = Field(default=None, max_length=120)
    duration_s: Annotated[float, Field(gt=0, le=8)] = 4.0
    evidence_ids: list[ID] = Field(default_factory=list, max_length=32)
    reason: str = Field(min_length=1, max_length=256)


class HoldIntent(Record):
    op: Literal['holding', 'return_live']
    evidence_ids: list[ID] = Field(default_factory=list, max_length=32)
    reason: str = Field(min_length=1, max_length=256)


class ReplayIntent(Record):
    op: Literal['replay']
    replay_id: ID
    transition: Literal['toast-wipe','ribbon-sweep','crumb-burst','iris-reveal'] = 'ribbon-sweep'
    evidence_ids: list[ID] = Field(min_length=1, max_length=32)
    reason: str = Field(min_length=1, max_length=256)


class UrgentReturnIntent(Record):
    op: Literal['urgent_return']
    slot: Annotated[int, Field(ge=1, le=5)]
    evidence_ids: list[ID] = Field(min_length=1, max_length=32)
    reason: str = Field(min_length=1, max_length=256)


DirectorIntent = Annotated[Abstention | CameraIntent | FramingIntent | GraphicIntent | HoldIntent | ReplayIntent | UrgentReturnIntent, Field(discriminator='op')]


class CommentaryIntent(Record):
    op: Literal['commentary']
    text: str = Field(min_length=1, max_length=240)
    basis: Literal['action', 'event_context'] = 'action'
    evidence_ids: list[ID] = Field(default_factory=list, max_length=32)
    reason: str = Field(min_length=1, max_length=256)


CommentatorIntent = Annotated[Abstention | CommentaryIntent, Field(discriminator='op')]


class FoundationSettings(BaseSettings):
    model_config = SettingsConfigDict(strict=True, extra='forbid', env_prefix='BREADCAST_FOUNDATION_', allow_inf_nan=False)
    configuration_revision: Positive = 1
    event: EventContext = Field(default_factory=lambda: EventContext(event_id='manual-event'))
    storage_directory: str = 'foundation-media'
    fixture_file: str | None = None
    providers: dict[Literal['storage', 'jobs', 'yolo', 'cosmos', 'search', 'llm', 'speech'], ProviderConfig] = Field(default_factory=dict)
    limits: Limits = Field(default_factory=Limits)
    direction: DirectionSettings = Field(default_factory=DirectionSettings)
    replay: ReplaySettings = Field(default_factory=ReplaySettings)

    @classmethod
    def load(cls, path):
        return cls.model_validate_json(path.read_text())


class ResolvedChunk(Record):
    manifest: ChunkManifest
    path: str


class ArchiveResolution(Record):
    source: SourceEpoch
    native: Interval
    available: Literal[True] = True
    mapping_revision: Positive | None
    chunks: list[ResolvedChunk] = Field(min_length=1,max_length=16)


class SearchHit(Record):
    scene: SceneEvent
    score: Annotated[float,Field(ge=0)]
    ranking: Literal['simulated','provider']
    index_version: str
    embedding_version: str
    watermark: int
    media: ArchiveResolution


class ContextSlice(Record):
    role: Literal['director','commentator','segmentor']
    snapshot: DecisionSnapshot
    target: dict
    event: EventContext | dict
    facts: dict[str,OfficialFact]
    observations: list[Observation]
    scenes: list[SceneEvent]
    aired: list[ProgramText]
    pending: list[ProgramText]
    archive_hits: list[dict]
    omitted: list[str]
    status: Literal['ready','truncated','unavailable']
    timing: Literal['source-local','unknown','calibrated-event']


class ShotIntent(Record):
    source: SourceEpoch
    native: Interval
    event: Interval | None = None
    mapping_revision: Positive | None = None
    scene_revisions: dict[ID, Positive] = Field(min_length=1, max_length=16)
    evidence_ids: list[ID] = Field(min_length=1, max_length=16)
    speed: Annotated[float, Field(gt=0)] = 1.0
    crop: CropRect = Field(default_factory=lambda: CropRect(x=0., y=0., width=1., height=1.))
    crop_space: Literal['oriented_source'] = 'oriented_source'
    edit: Literal['continuous', 'repeat'] = 'continuous'
    reason: str = Field(min_length=1, max_length=256)

    @model_validator(mode='after')
    def timing(self):
        if self.speed not in (0.5, 1.0, 2.0):
            raise ValueError('Use 0.5, 1, or 2 speed')
        if (self.event is None) != (self.mapping_revision is None):
            raise ValueError('Event interval and mapping revision must appear together')
        duration=(self.native.end-self.native.start)*float(Fraction(self.source.time_base))
        if not .2-1e-9 <= duration <= 6.+1e-9:
            raise ValueError('A shot must cover 0.2–6 native seconds')
        if len(set(self.evidence_ids))!=len(self.evidence_ids):
            raise ValueError('Duplicate evidence references')
        return self


class SegmentWait(Record):
    op: Literal['wait']
    source: SourceEpoch
    required: Interval
    reason: str = Field(min_length=1, max_length=256)


class SegmentPlan(Record):
    op: Literal['plan']
    source: SourceEpoch
    action: Interval
    required: Interval
    shots: list[ShotIntent] = Field(min_length=1, max_length=12)
    reason: str = Field(min_length=1, max_length=256)

    @model_validator(mode='after')
    def action_bounds(self):
        if not self.required.start <= self.action.start < self.action.end <= self.required.end:
            raise ValueError('Required interval must preserve the complete action')
        return self


SegmentorIntent = Annotated[Abstention | SegmentWait | SegmentPlan, Field(discriminator='op')]


class SegmentorResult(Record):
    payload: SegmentorIntent
    snapshot: DecisionSnapshot
    origin: Literal['fixture', 'provider']
    model_id: str = Field(min_length=1, max_length=192)
    model_version: str = Field(min_length=1, max_length=128)

    @model_validator(mode='after')
    def budget(self):
        if len(self.model_dump_json().encode()) > 65536:
            raise ValueError('Segmentor output exceeds 64 KiB')
        return self


class ReplayOutput(Record):
    width: Literal[640] = 640
    height: Literal[360] = 360
    fps_num: Literal[15] = 15
    fps_den: Literal[1] = 1

    @field_validator('width','height','fps_num','fps_den',mode='before')
    @classmethod
    def exact_integer(cls, value):
        if type(value) is not int:raise ValueError('Output format fields must be integers')
        return value


class ReplayPlan12(Record):
    schema_version: Literal['1.2'] = '1.2'
    plan_id: ID
    event_id: ID
    run_id: ID
    context_revision: Positive
    configuration_revision: Positive
    snapshot: DecisionSnapshot
    input_kind: Literal['archive', 'live_buffer']
    source: SourceEpoch
    action: Interval
    required: Interval
    expires_at: float
    shots: list[ShotIntent] = Field(min_length=1, max_length=12)
    transition: Literal['cut'] = 'cut'
    audio_policy: Literal['mute_source_and_live_audio'] = 'mute_source_and_live_audio'
    replay_marker: Literal[True] = True
    score_overlay: Literal['hidden'] = 'hidden'
    output: ReplayOutput = Field(default_factory=ReplayOutput)
    selection_reason: str = Field(min_length=1, max_length=256)
    expected_duration_ms: Annotated[float, Field(gt=0, le=12000)] | None = None

    @field_validator('replay_marker',mode='before')
    @classmethod
    def exact_marker(cls, value):
        if value is not True:raise ValueError('Replay marker must be true')
        return value

    @model_validator(mode='after')
    def ownership(self):
        if (self.event_id,self.run_id,self.context_revision,self.configuration_revision) != (
            self.snapshot.event_id,self.snapshot.run_id,self.snapshot.context_revision,self.snapshot.configuration_revision):
            raise ValueError('Plan ownership differs from reviewed snapshot')
        if any((s.source.event_id,s.source.run_id)!=(self.event_id,self.run_id) for s in self.shots):
            raise ValueError('Shot ownership differs from plan')
        if (self.source.event_id,self.source.run_id)!=(self.event_id,self.run_id):
            raise ValueError('Action ownership differs from plan')
        SegmentPlan(op='plan',source=self.source,action=self.action,required=self.required,shots=self.shots,reason=self.selection_reason)
        if self.shots[0].edit!='continuous':
            raise ValueError('First shot must be continuous')
        if any(s.event is None for s in self.shots):
            if any(s.event is not None or s.source!=self.source or s.edit!='continuous' for s in self.shots):
                raise ValueError('Source-local replay requires one original source and no repeats')
        for previous,current in zip(self.shots,self.shots[1:]):
            a,b=previous.event or previous.native,current.event or current.native
            if current.edit=='continuous' and a.end!=b.start:
                raise ValueError('Continuous cuts must meet at the same timestamp')
        for i,s in enumerate(self.shots):
            if s.edit=='repeat' and not any(p.source.source_id!=s.source.source_id and p.event and s.event and
                p.event.start<=s.event.start<s.event.end<=p.event.end for p in self.shots[:i]):
                raise ValueError('Repeat requires an earlier interval on another source')
        return self


class ReplayCandidate(Record):
    candidate_id: ID
    scene_id: ID
    scene_revision: Positive
    source: SourceEpoch
    purpose: Literal['automatic', 'recall']
    admitted_utc: float
    deadline_utc: float
    preparation_id: ID | None = None
    state: Literal['waiting', 'queued', 'preparing', 'ready', 'skipped', 'failed', 'canceled', 'aired'] = 'waiting'
    reason: str = Field(default='', max_length=256)


class PublicSearchRequest(Record):
    id: ID
    run_id: ID
    text: str = Field(min_length=1, max_length=1024)
    limit: Annotated[int, Field(ge=1, le=10)] = 5


class PublicSearchHit(Record):
    scene_id: ID
    scene_revision: Positive
    source: SourceEpoch
    native: Interval
    event: Interval | None = None
    description: str = Field(max_length=2048)
    claim_kind: Literal['observed', 'inferred', 'unknown'] = 'unknown'
    score: Annotated[float, Field(ge=0)]
    ranking: Literal['simulated', 'provider']
    available: bool
    reason: str | None = Field(default=None, max_length=256)
    index_version: str = Field(max_length=128)
    embedding_version: str = Field(max_length=128)


class SourceFreshness(Record):
    source: SourceEpoch
    watermark: int


class PublicSearchResult(Record):
    id: ID
    run_id: ID
    hits: list[PublicSearchHit] = Field(max_length=10)
    freshness: list[SourceFreshness] = Field(max_length=5)
    ranking: Literal['simulated', 'provider']
    reason: str | None = Field(default=None, max_length=256)
