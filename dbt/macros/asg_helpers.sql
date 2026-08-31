{#
    Small, dependency-free helpers.

    These are written in plain BigQuery SQL on purpose: the models stay runnable
    even on a machine where `dbt deps` could not reach hub.getdbt.com (a common
    situation behind a corporate proxy).
#}


{#
    Deterministic surrogate key over an arbitrary list of columns.

    Equivalent to dbt_utils.generate_surrogate_key but with no package
    dependency. NULLs are coalesced to a sentinel so that two rows differing
    only by "NULL vs empty string" still hash differently from each other.
#}
{% macro asg_surrogate_key(field_list) -%}

    {%- set fields = [] -%}
    {%- for field in field_list -%}
        {%- do fields.append(
            "coalesce(cast(" ~ field ~ " as string), '_asg_null_')"
        ) -%}
    {%- endfor -%}

    to_hex(md5({{ fields | join(" || '|' || " ) }}))

{%- endmacro %}


{#
    Audit columns stamped onto every model so any row can be traced back to the
    run that produced it. Mirrors the _silver_loaded_at / _gold_loaded_at
    columns already present in gold_dbt.fact_financial.
#}
{% macro asg_audit_columns(layer) -%}

    current_timestamp()                as _{{ layer }}_loaded_at,
    '{{ invocation_id }}'              as _dbt_invocation_id,
    '{{ target.name }}'                as _dbt_target

{%- endmacro %}


{#
    Cents-safe money cast. BigQuery FLOAT64 cannot represent IDR amounts
    exactly once they get large, so money always lands in NUMERIC.
#}
{% macro asg_money(column_name) -%}
    cast({{ column_name }} as numeric)
{%- endmacro %}
