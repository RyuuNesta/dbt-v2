{{
    config(
        materialized = 'view'
    )
}}

/*
    SILVER - cleaned and conformed.

    This is your original stg_gl_entries.sql logic, moved to the layer where it
    belongs and hardened in four places:

      1. Deduplication. Bronze is append-only, so a re-ingested file would
         duplicate document_number. We keep the most recently loaded row per
         business key.
      2. transaction_category had no ELSE branch, so any document_type outside
         SA / KR / DR silently became NULL. It is now labelled 'Unmapped' and
         flagged, which surfaces the problem instead of hiding it.
      3. debit_amount / credit_amount were derived from the sign of
         amount_local while the source also ships an explicit debit_credit
         flag. We now trust the flag and raise _has_sign_conflict when the two
         disagree, rather than letting one silently win.
      4. Period columns are added up front so gold never has to re-derive them
         (matches period_date / period_month / period_year in
         gold_dbt.fact_financial).
*/

with bronze as (

    select * from {{ ref('bronze_gl_entries') }}

),

deduplicated as (

    select
        *,
        row_number() over (
            partition by gl_entry_key
            order by _bronze_loaded_at desc
        ) as _row_recency

    from bronze

),

latest_only as (

    select * except (_row_recency)
    from deduplicated
    where _row_recency = 1

),

cleaned as (

    select
        -- ---------------------------------------------------------------
        -- keys
        -- ---------------------------------------------------------------
        gl_entry_key,
        document_number,
        company_code,
        fiscal_year,

        -- ---------------------------------------------------------------
        -- period grain (pre-derived for gold)
        -- ---------------------------------------------------------------
        posting_date,
        date_trunc(posting_date, month)             as period_month,
        extract(year  from posting_date)            as period_year,
        extract(quarter from posting_date)          as period_quarter,

        -- ---------------------------------------------------------------
        -- descriptors
        -- ---------------------------------------------------------------
        document_type,
        gl_account,
        nullif(trim(cost_center), '')               as cost_center,
        upper(trim(currency))                       as currency,
        upper(trim(debit_credit))                   as debit_credit,
        nullif(trim(vendor_customer), '')           as vendor_customer,
        nullif(trim(description), '')               as description,

        case document_type
            when 'SA' then 'GL Posting'
            when 'KR' then 'Vendor Invoice'
            when 'DR' then 'Customer Invoice'
            else 'Unmapped'
        end                                         as transaction_category,

        case
            when gl_account between 400000 and 499999 then 'Revenue'
            when gl_account between 500000 and 599999 then 'Expense'
            when gl_account between 100000 and 199999 then 'Receivable'
            else 'Other'
        end                                         as account_group,

        -- ---------------------------------------------------------------
        -- measures - driven by the explicit debit_credit flag
        -- ---------------------------------------------------------------
        amount_local,
        abs(amount_local)                           as amount_abs,

        case
            when upper(trim(debit_credit)) = 'D' then abs(amount_local)
            else cast(0 as numeric)
        end                                         as debit_amount,

        case
            when upper(trim(debit_credit)) = 'C' then abs(amount_local)
            else cast(0 as numeric)
        end                                         as credit_amount,

        -- ---------------------------------------------------------------
        -- quality flags (surface problems, never drop rows)
        -- ---------------------------------------------------------------
        document_type not in ('SA', 'KR', 'DR')     as _has_unmapped_doc_type,

        (
            (upper(trim(debit_credit)) = 'D' and amount_local < 0)
            or
            (upper(trim(debit_credit)) = 'C' and amount_local > 0)
        )                                           as _has_sign_conflict,

        vendor_customer is null
            or trim(vendor_customer) = ''           as _is_missing_counterparty,

        amount_local = 0                            as _is_zero_amount,

        -- ---------------------------------------------------------------
        -- lineage
        -- ---------------------------------------------------------------
        _source_relation,
        _bronze_loaded_at,
        {{ asg_audit_columns('silver') }}

    from latest_only

)

select * from cleaned
