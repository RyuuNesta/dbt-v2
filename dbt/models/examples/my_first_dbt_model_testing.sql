{{
    config(
        materialized = 'view'
    )
}}

/*
    Starter smoke-test model.

    This file was referenced by your schema.yml and by
    my_second_dbt_model_testing.sql but did not exist on disk, so the project
    could not compile. Recreated here so the ref resolves.

    The `where id is not null` filter is deliberately left ON. dbt's stock
    scaffold ships it commented out, which makes the not_null test fail on
    purpose as a teaching device - not something you want in a project the whole
    data team runs.
*/

with source_data as (

    select 1 as id
    union all
    select null as id

)

select *
from source_data
where id is not null
