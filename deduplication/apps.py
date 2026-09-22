from django.apps import AppConfig

DEFAULT_CONFIG = {
    "gql_create_deduplication_review_perms": ["172001"],
    "gql_create_deduplication_payment_review_perms": ["172002"],
    "gql_resolve_duplicate_perms": ["172003"],
    "gql_run_scan_perms": ["172004"],
    "gql_query_duplicates_perms": ["172005"],
    "subject_model": "individual.Individual",
    "demographic_columns": ["first_name", "last_name", "dob"],
    "identifier_keys": [],
    "merge_policy": "delete",
}


class DeduplicationConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'deduplication'

    gql_create_deduplication_review_perms = None
    gql_create_deduplication_payment_review_perms = None
    gql_resolve_duplicate_perms = None
    gql_run_scan_perms = None
    gql_query_duplicates_perms = None
    subject_model = None
    demographic_columns = None
    identifier_keys = None
    merge_policy = None

    def ready(self):
        from core.models import ModuleConfiguration

        cfg = ModuleConfiguration.get_or_default(self.name, DEFAULT_CONFIG)
        self.__load_config(cfg)
        self.__register_builtin_sources()

    @classmethod
    def __load_config(cls, cfg):
        """
        Load all config fields that match current AppConfig class fields, all custom fields have to be loaded separately
        """
        for field in cfg:
            if hasattr(DeduplicationConfig, field):
                setattr(DeduplicationConfig, field, cfg[field])

    @classmethod
    def __register_builtin_sources(cls):
        from deduplication.sources import register
        from deduplication.sources.demographic import DemographicSource
        from deduplication.sources.identifier import IdentifierSource

        register(DemographicSource())
        register(IdentifierSource())
