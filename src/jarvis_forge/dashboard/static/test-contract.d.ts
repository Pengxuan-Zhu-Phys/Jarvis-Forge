/** Consumer types for Forge-owned contract v1 pinned in contracts/v1; sync with scripts/sync-forge-contract.py. */
export type State = 'queued'|'running'|'finished'|'interrupted'|'not_run';
export type Outcome = 'passed'|'failed'|'error'|'skipped'|'xfailed'|'xpassed'|null;
export interface AdapterCapability {adapter:string;enumeration:'exact'|'cached'|'none';progress:'case_events'|'step_events'|'final_only';logs:('case_captured'|'case_live'|'step_only'|'none')[];cancellation:'supported'|'unsupported';artifacts:('case_bound'|'step_bound')[];}
export interface Capabilities {api_version:1;features:{test_cases:true};adapters:AdapterCapability[];inventories?:Record<string,{id:string}>;}
export interface ExecutionStep {id:string;status:string;seconds?:number;kind?:string;adapter?:string;}
export interface Criterion {criterion_id:string;expected:string;verification:'automated'|'manual';status:'pending'|'passed'|'failed'|'unobserved'|'not_applicable';observed:unknown|null;evidence_refs:string[];}
export interface TestCase {instance_id:string|null;case_id:string;definition_id:string|null;native_id:string;title:string;adapter:string;variant_key:string;variant_count?:number;parameters:Record<string,unknown>;file?:string;component?:string;tags?:string[];target_id?:string;step_id?:string;state:State;outcome:Outcome;execution_mode:'executed'|'reused';attempt:number;attempts?:number[];metadata_status:string;purpose?:string;scenario?:{preconditions?:string;inputs?:unknown;action?:string};criteria?:Criterion[];failure_meaning?:string;limitations?:string[];seconds?:number|null;report_status?:'provisional'|'ready'|'failed';reused_from?:{job_id:string;instance_id:string;attempt:number};}
export interface ProgressSummary {unit:'test_case'|'step'|string;project_id?:string;target_id?:string;adapter?:string;total:number|null;completed:number;running:number;pending:number;interrupted:number;not_run:number;outcomes:Partial<Record<Exclude<Outcome,null>,number>>;reused:number;total_confirmed:boolean;}
export interface ProgressSnapshot {ok:boolean;api_version:1;job_id:string;revision:number;updated_at:string;summaries:ProgressSummary[];steps:ExecutionStep[];current_test?:TestCase|null;}
export interface TestPage {ok:boolean;api_version:1;inventory_revision:string;items:TestCase[];next_cursor:string|null;total?:number|null;}
export interface Report {status:'provisional'|'ready'|'failed';html_href:string|null;generation_error:string|null;metadata_digest?:string|null;original_html_href?:string|null;render_revision?:number;template_version:string;outcome?:Outcome;}
export interface LogChunk {text:string;next_offset:number;eof:boolean;truncated:boolean;available:boolean;scope:'case'|'step';}
export interface ApiError {message:string;code?:string;details?:unknown;}
