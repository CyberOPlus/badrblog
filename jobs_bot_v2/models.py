from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class JobCandidate:
    source_name: str
    source_url: str
    canonical_url: str
    title: str
    company: str = ""
    location: str = ""
    country: str = ""
    contract_type: str = ""
    salary: str = ""
    deadline: str = ""
    published_at: str = ""
    description: str = ""
    application_url: str = ""
    number_of_positions: int = 0
    diploma: str = ""
    experience: str = ""
    documents_required: list[str] = field(default_factory=list)
    source_priority: str = ""
    entry_level: bool = False
    remote: bool = False
    visa_sponsorship: bool = False
    relocation: bool = False
    eligibility: str = "unknown"
    official_source: bool = False
    requires_attribution: bool = False
    main_image: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


@dataclass
class ArticlePackage:
    title: str
    seo_title: str
    meta_description: str
    slug: str
    html: str
    card_title: str
    labels: list[str]
    facts: dict[str, Any]
    source_url: str
    application_url: str

    def to_dict(self):
        return asdict(self)


@dataclass
class SocialPackage:
    facebook_post: str
    first_comment: str
    card_title: str

    def to_dict(self):
        return asdict(self)
