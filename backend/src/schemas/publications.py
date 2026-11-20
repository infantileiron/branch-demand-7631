from pydantic import BaseModel, Field
from src.constants.privacy import (DEFAULT_CLIP_UPPER, DEFAULT_DAILY_CAP, DEFAULT_PER_RELEASE_EPSILON,
                                   MAX_CLIP_UPPER, MAX_DAILY_CAP, MAX_EPSILON, MIN_EPSILON)
from src.schemas.business import Branch


class PublicationInput(BaseModel):
    number: str = Field(min_length=6, max_length=64, pattern=r'^[A-Za-z0-9][A-Za-z0-9._:-]{5,63}$')
    name: str = Field(default='', max_length=100)
    branch: Branch
    run_id: int = Field(gt=0)
    epsilon: float = Field(default=DEFAULT_PER_RELEASE_EPSILON, ge=MIN_EPSILON, le=MAX_EPSILON, allow_inf_nan=False)
    daily_cap: float = Field(default=DEFAULT_DAILY_CAP, ge=MIN_EPSILON, le=MAX_DAILY_CAP, allow_inf_nan=False)
    clip_upper: int = Field(default=DEFAULT_CLIP_UPPER, ge=1, le=MAX_CLIP_UPPER)
