{#
    Target-aware dataset routing for the medallion layers.

    dbt's default behaviour is to concatenate:  <target.dataset>_<custom schema>
    That is a good safety net in development but it means production can never
    land on the exact dataset names the business already reads from
    (bronze_dbt / silver_dbt / gold_dbt).

    So we branch on the target:

      target: prod        -> use the mapped production dataset verbatim
                             bronze -> bronze_dbt
                             silver -> silver_dbt
                             gold   -> gold_dbt
                             (anything else falls through unmapped)

      any other target    -> <target.dataset>_<custom schema>
                             e.g. dbt_dev_bronze, dbt_testing_silver

    Net effect: a developer running `dbt build` on the dev target physically
    cannot overwrite a production table, while `dbt build --target prod` from
    the orchestrator writes exactly where dbt Cloud used to write.
#}

{% macro generate_schema_name(custom_schema_name, node) -%}

    {%- set default_schema = target.schema -%}

    {#- Production dataset map. Add new layers here, not in the models. -#}
    {%- set prod_schema_map = {
        'bronze': 'bronze_dbt',
        'silver': 'silver_dbt',
        'gold':   'gold_dbt'
    } -%}

    {%- if custom_schema_name is none -%}

        {{ default_schema }}

    {%- elif target.name == 'prod' -%}

        {%- set layer = custom_schema_name | trim | lower -%}
        {{ prod_schema_map.get(layer, layer) }}

    {%- else -%}

        {{ default_schema }}_{{ custom_schema_name | trim }}

    {%- endif -%}

{%- endmacro %}
