{{
    config(
        materialized     = 'table',
        partition_by     = {
            'field': 'period_month',
            'data_type': 'date',
            'granularity': 'month'
        },
        cluster_by       = ['company_code', 'account_group']
    )
}}

/*
    GOLD - business-facing aggregate.

    Grain: one row per company_code x period_month x account_group x
           transaction_category.

    Shaped to match the conventions already in gold_dbt.kpi_monthly and
    gold_dbt.fact_financial: month-partitioned, MTD / YTD measures, audit
    timestamps carried through from the layer below.
*/

with silver as (

    select * from {{ ref('silver_gl_entries') }}

),

monthly as (

    select
        company_code,
        period_month,
        period_year,
        period_quarter,
        account_group,
        transaction_category,

        count(*)                                as entry_count,
        count(distinct document_number)          as document_count,
        count(distinct vendor_customer)          as counterparty_count,

        sum(debit_amount)                       as debit_amount_mtd,
        sum(credit_amount)                      as credit_amount_mtd,
        sum(debit_amount) - sum(credit_amount)  as net_amount_mtd,
        sum(amount_abs)                         as gross_amount_mtd,

        avg(amount_abs)                         as avg_entry_amount,
        max(amount_abs)                         as max_entry_amount,

        countif(_has_unmapped_doc_type)         as unmapped_doc_type_count,
        countif(_has_sign_conflict)             as sign_conflict_count,
        countif(_is_missing_counterparty)       as missing_counterparty_count,

        max(_silver_loaded_at)                  as _silver_loaded_at

    from silver

    group by
        company_code,
        period_month,
        period_year,
        period_quarter,
        account_group,
        transaction_category

),

with_running_totals as (

    select
        *,

        sum(debit_amount_mtd) over (
            partition by company_code, period_year, account_group, transaction_category
            order by period_month
            rows between unbounded preceding and current row
        )                                       as debit_amount_ytd,

        sum(credit_amount_mtd) over (
            partition by company_code, period_year, account_group, transaction_category
            order by period_month
            rows between unbounded preceding and current row
        )                                       as credit_amount_ytd,

        sum(net_amount_mtd) over (
            partition by company_code, period_year, account_group, transaction_category
            order by period_month
            rows between unbounded preceding and current row
        )                                       as net_amount_ytd

    from monthly

)

select
    {{ asg_surrogate_key([
        'company_code',
        'period_month',
        'account_group',
        'transaction_category'
    ]) }}                                       as gl_summary_key,

    with_running_totals.*,

    net_amount_mtd >= 0                         as _is_positive_mtd,
    net_amount_ytd >= 0                         as _is_positive_ytd,
    sign_conflict_count = 0                     as _is_sign_clean,

    {{ asg_audit_columns('gold') }}

from with_running_totals
