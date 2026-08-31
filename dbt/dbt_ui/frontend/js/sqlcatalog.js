/* ==========================================================================
   sqlcatalog.js - static GoogleSQL vocabulary for autocomplete.

   Held on the frontend deliberately: it never changes between sessions, so a
   round trip to fetch it would only add latency to the first keystroke.

   Scoped to BigQuery / GoogleSQL rather than generic ANSI SQL, so the
   suggestions match the dialect this project actually runs. Snippets use ${}
   placeholders; the editor drops the caret at the first one.
   ========================================================================== */

/* ------------------------------------------------------------- keywords --- */

export const KEYWORDS = [
  // clauses
  'select', 'from', 'where', 'group by', 'having', 'order by', 'limit',
  'offset', 'qualify', 'window', 'with', 'as', 'distinct', 'all',
  // joins
  'join', 'inner join', 'left join', 'left outer join', 'right join',
  'full outer join', 'cross join', 'on', 'using', 'unnest',
  // set operations
  'union all', 'union distinct', 'intersect distinct', 'except distinct',
  // predicates
  'and', 'or', 'not', 'in', 'exists', 'between', 'like', 'ilike',
  'is null', 'is not null', 'is true', 'is false',
  // conditionals
  'case', 'when', 'then', 'else', 'end', 'if',
  // windowing
  'over', 'partition by', 'rows between', 'range between',
  'unbounded preceding', 'unbounded following', 'current row',
  // ordering
  'asc', 'desc', 'nulls first', 'nulls last',
  // types in cast expressions
  'cast', 'safe_cast', 'int64', 'float64', 'numeric', 'bignumeric', 'bool',
  'string', 'bytes', 'date', 'datetime', 'time', 'timestamp', 'array',
  'struct', 'json', 'geography', 'interval',
  // misc
  'pivot', 'unpivot', 'tablesample', 'recursive', 'except', 'replace',
];

/* ------------------------------------------------------------ functions --- */
/* label, snippet, one-line description. Grouped by purpose in the comments so
   this stays maintainable, but flattened for lookup. */

export const FUNCTIONS = [
  // aggregate
  ['count', 'count(${1:*})', 'Number of rows'],
  ['countif', 'countif(${1:condition})', 'Rows where the condition is true'],
  ['sum', 'sum(${1:expr})', 'Total'],
  ['avg', 'avg(${1:expr})', 'Mean'],
  ['min', 'min(${1:expr})', 'Smallest value'],
  ['max', 'max(${1:expr})', 'Largest value'],
  ['any_value', 'any_value(${1:expr})', 'An arbitrary non-null value'],
  ['array_agg', 'array_agg(${1:expr})', 'Collect values into an array'],
  ['string_agg', 'string_agg(${1:expr}, ${2:", "})', 'Join values into one string'],
  ['approx_count_distinct', 'approx_count_distinct(${1:expr})',
   'Cheap distinct estimate for very large inputs'],

  // window
  ['row_number', 'row_number() over (partition by ${1:key} order by ${2:col} desc)',
   'Sequential number within a partition. The standard way to deduplicate'],
  ['rank', 'rank() over (order by ${1:col})', 'Ranking with gaps on ties'],
  ['dense_rank', 'dense_rank() over (order by ${1:col})', 'Ranking without gaps'],
  ['lag', 'lag(${1:col}) over (order by ${2:col})', 'Value from the previous row'],
  ['lead', 'lead(${1:col}) over (order by ${2:col})', 'Value from the next row'],
  ['first_value', 'first_value(${1:col}) over (order by ${2:col})', 'First value in the window'],
  ['ntile', 'ntile(${1:4}) over (order by ${2:col})', 'Split rows into buckets'],

  // null handling
  ['coalesce', 'coalesce(${1:expr}, ${2:fallback})', 'First non-null argument'],
  ['ifnull', 'ifnull(${1:expr}, ${2:fallback})', 'Fallback when null'],
  ['nullif', 'nullif(${1:expr}, ${2:value})', 'Null when the two are equal. Use to turn "" into null'],
  ['safe_divide', 'safe_divide(${1:numerator}, ${2:denominator})',
   'Division that returns null instead of erroring on zero'],

  // conditional
  ['if', 'if(${1:condition}, ${2:then}, ${3:else})', 'Inline conditional'],
  ['greatest', 'greatest(${1:a}, ${2:b})', 'Largest of the arguments'],
  ['least', 'least(${1:a}, ${2:b})', 'Smallest of the arguments'],

  // date and time
  ['current_date', 'current_date()', "Today's date"],
  ['current_timestamp', 'current_timestamp()', 'Now, as a timestamp'],
  ['date_trunc', 'date_trunc(${1:date_col}, ${2:month})',
   'Round a date down to a period. month, quarter, year, week'],
  ['timestamp_trunc', 'timestamp_trunc(${1:ts_col}, ${2:day})', 'Round a timestamp down'],
  ['date_diff', 'date_diff(${1:later}, ${2:earlier}, ${3:day})', 'Difference between two dates'],
  ['date_add', 'date_add(${1:date_col}, interval ${2:1} ${3:day})', 'Shift a date forward'],
  ['date_sub', 'date_sub(${1:date_col}, interval ${2:1} ${3:day})', 'Shift a date backward'],
  ['extract', 'extract(${1:year} from ${2:date_col})', 'Pull one part out of a date'],
  ['format_date', 'format_date(${1:"%Y-%m"}, ${2:date_col})', 'Format a date as a string'],
  ['parse_date', 'parse_date(${1:"%Y-%m-%d"}, ${2:string_col})', 'Parse a string into a date'],
  ['last_day', 'last_day(${1:date_col}, ${2:month})', 'Final day of the period'],

  // string
  ['concat', 'concat(${1:a}, ${2:b})', 'Join strings'],
  ['trim', 'trim(${1:expr})', 'Remove surrounding whitespace'],
  ['ltrim', 'ltrim(${1:expr})', 'Remove leading whitespace'],
  ['rtrim', 'rtrim(${1:expr})', 'Remove trailing whitespace'],
  ['upper', 'upper(${1:expr})', 'Upper case'],
  ['lower', 'lower(${1:expr})', 'Lower case'],
  ['length', 'length(${1:expr})', 'Character count'],
  ['substr', 'substr(${1:expr}, ${2:1}, ${3:10})', 'Extract a portion'],
  ['split', 'split(${1:expr}, ${2:","})', 'Break a string into an array'],
  ['replace', 'replace(${1:expr}, ${2:from}, ${3:to})', 'Substitute text'],
  ['regexp_extract', 'regexp_extract(${1:expr}, ${2:r"pattern"})', 'First regex match'],
  ['regexp_replace', 'regexp_replace(${1:expr}, ${2:r"pattern"}, ${3:replacement})',
   'Regex substitution'],
  ['regexp_contains', 'regexp_contains(${1:expr}, ${2:r"pattern"})', 'Regex test'],
  ['starts_with', 'starts_with(${1:expr}, ${2:prefix})', 'Prefix test'],
  ['ends_with', 'ends_with(${1:expr}, ${2:suffix})', 'Suffix test'],
  ['lpad', 'lpad(${1:expr}, ${2:8}, ${3:"0"})', 'Left-pad to a fixed width'],
  ['to_hex', 'to_hex(${1:bytes_expr})', 'Bytes as a hex string'],
  ['md5', 'md5(${1:expr})', 'MD5 digest. Wrap in to_hex for a readable key'],
  ['farm_fingerprint', 'farm_fingerprint(${1:expr})', 'Fast 64-bit hash'],
  ['generate_uuid', 'generate_uuid()', 'A new random UUID'],

  // numeric
  ['round', 'round(${1:expr}, ${2:2})', 'Round to N decimal places'],
  ['floor', 'floor(${1:expr})', 'Round down'],
  ['ceil', 'ceil(${1:expr})', 'Round up'],
  ['abs', 'abs(${1:expr})', 'Absolute value'],
  ['sign', 'sign(${1:expr})', '-1, 0 or 1'],
  ['mod', 'mod(${1:a}, ${2:b})', 'Remainder'],
  ['div', 'div(${1:a}, ${2:b})', 'Integer division'],

  // array and struct
  ['array_length', 'array_length(${1:array_col})', 'Element count'],
  ['array_to_string', 'array_to_string(${1:array_col}, ${2:","})', 'Array as a string'],
  ['generate_array', 'generate_array(${1:1}, ${2:10})', 'Array of a numeric range'],
  ['generate_date_array', 'generate_date_array(${1:start}, ${2:end}, interval 1 day)',
   'Array of dates. Useful for a date spine'],
  ['struct', 'struct(${1:expr} as ${2:name})', 'Build a nested record'],

  // casting and safety
  ['cast', 'cast(${1:expr} as ${2:string})', 'Convert type, errors on failure'],
  ['safe_cast', 'safe_cast(${1:expr} as ${2:int64})', 'Convert type, null on failure'],
].map(([label, snippet, detail]) => ({ label, snippet, detail }));

/* ----------------------------------------------------------- dbt jinja --- */

export const DBT_SNIPPETS = [
  {
    label: "ref()",
    snippet: "{{ ref('${1:model_name}') }}",
    detail: 'Reference another dbt model. Resolves to the right dataset per environment',
  },
  {
    label: "source()",
    snippet: "{{ source('${1:source_name}', '${2:table_name}') }}",
    detail: 'Reference a declared source table',
  },
  {
    label: "var()",
    snippet: "{{ var('${1:variable_name}') }}",
    detail: 'Read a variable from dbt_project.yml',
  },
  {
    label: 'target.name',
    snippet: '{{ target.name }}',
    detail: 'The active environment name',
  },
  {
    label: 'target.dataset',
    snippet: '{{ target.dataset }}',
    detail: 'The active default dataset',
  },
  {
    label: 'config()',
    snippet: "{{ config(materialized = '${1:view}') }}",
    detail: 'Set model configuration inline',
  },
  {
    label: 'is_incremental()',
    snippet: '{% if is_incremental() %}\n  where ${1:updated_at} > (select max(${1:updated_at}) from {{ this }})\n{% endif %}',
    detail: 'Guard for the incremental branch of a model',
  },
  {
    label: 'this',
    snippet: '{{ this }}',
    detail: 'The relation this model writes to',
  },
];

/* ------------------------------------------------------------ assembly --- */

/** Category identifiers, in the order the dropdown shows them. */
export const CATEGORY_ORDER = ['column', 'table', 'macro', 'function', 'keyword'];

export const CATEGORY_LABELS = {
  column: 'Columns',
  table: 'Tables & models',
  macro: 'dbt',
  function: 'Functions',
  keyword: 'Keywords',
};

/** Static suggestion objects, built once at module load. */
export const KEYWORD_ITEMS = KEYWORDS.map((word) => ({
  label: word,
  insert: word,
  category: 'keyword',
  meta: 'keyword',
}));

export const FUNCTION_ITEMS = FUNCTIONS.map((fn) => ({
  label: fn.label,
  snippet: fn.snippet,
  category: 'function',
  meta: 'function',
  detail: fn.detail,
}));

export const DBT_ITEMS = DBT_SNIPPETS.map((item) => ({
  label: item.label,
  snippet: item.snippet,
  category: 'macro',
  meta: 'dbt',
  detail: item.detail,
}));

/**
 * Expand a ${1:placeholder} snippet.
 * Returns the literal text plus where the caret should land.
 */
export function expandSnippet(snippet) {
  const first = snippet.match(/\$\{(\d+):([^}]*)\}/);
  const text = snippet.replace(/\$\{\d+:([^}]*)\}/g, '$1');

  if (!first) return { text, caret: text.length, selectionLength: 0 };

  const before = snippet.slice(0, first.index).replace(/\$\{\d+:([^}]*)\}/g, '$1');
  return { text, caret: before.length, selectionLength: first[2].length };
}
