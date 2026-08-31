{{
    config(
        materialized = 'table',
        cluster_by   = ['company_code', 'fiscal_year']
    )
}}

/*
    BRONZE - raw landing zone.

    Contract for this layer:
      * one row in, one row out - no filtering, no deduplication, no business
        logic. Whatever the source system sent is what lands here.
      * light typing only, so downstream layers are not re-parsing strings.
      * audit columns appended so every row is traceable to an invocation.

    Anything that reshapes meaning (deduplication, categorisation, sign
    handling) belongs in silver, not here. Keeping bronze faithful is what lets
    you replay history after a business rule changes.
*/

with source as (

    select * from {{ ref('gl_entries') }}

),

renamed as (

    select
        -- ---------------------------------------------------------------
        -- keys
        -- ---------------------------------------------------------------
        document_number,
        company_code,
        fiscal_year,

        -- ---------------------------------------------------------------
        -- dates
        -- ---------------------------------------------------------------
        cast(posting_date as date)          as posting_date,

        -- ---------------------------------------------------------------
        -- descriptors
        -- ---------------------------------------------------------------
        document_type,
        gl_account,
        cost_center,
        currency,
        debit_credit,
        vendor_customer,
        description,

        -- ---------------------------------------------------------------
        -- measures
        -- ---------------------------------------------------------------
        {{ asg_money('amount_local') }}     as amount_local

    from source

)

select
    {{ asg_surrogate_key(['document_number', 'company_code', 'fiscal_year']) }}
        as gl_entry_key,

    renamed.*,

    'seed.gl_entries'                       as _source_relation,
    {{ asg_audit_columns('bronze') }}

from renamed
