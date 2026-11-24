export class ApiError extends Error {
  status:number; code?:string; requestId?:string; details:unknown;
  constructor(message:string,status:number,code?:string,requestId?:string,details?:unknown){super(message);this.name='ApiError';this.status=status;this.code=code;this.requestId=requestId;this.details=details;}
}
export async function api<T=any>(path:string, options:RequestInit={}):Promise<T>{
  const headers = new Headers(options.headers);
  if(options.body && !(options.body instanceof FormData)) headers.set('Content-Type','application/json');
  headers.set('Accept','application/json');
  let response:Response;
  try{response=await fetch(`/api${path}`,{...options,headers,credentials:'same-origin',cache:'no-store'});}
  catch(error){throw new ApiError(error instanceof DOMException&&error.name==='AbortError'?'Запрос отменён. Повторите действие.':'Сервер недоступен. Проверьте, запущено ли приложение, и повторите попытку.',0,'network_error');}
  if(response.status===204)return undefined as T;
  const requestId=response.headers.get('x-request-id')||undefined;
  const contentType=response.headers.get('content-type')||'';
  let value:any;
  if(contentType.includes('json')){try{value=await response.json();}catch{throw new ApiError('Сервер вернул повреждённый ответ. Повторите запрос; если ошибка сохранится, передайте её администратору.',response.status,'invalid_json',requestId);}}
  else throw new ApiError(response.ok?'Открыта страница без работающего API. Запустите приложение через start.bat и откройте адрес, указанный сервером.':`Сервер вернул HTTP ${response.status} без описания ошибки. Проверьте журнал приложения.`,response.status,'unexpected_response',requestId);
  if(!response.ok){
    const detail=value?.detail??value;
    const message=typeof detail==='string'?detail:typeof detail?.message==='string'?detail.message:Array.isArray(detail)?detail.map((item:any)=>`${(item.loc||[]).filter((part:unknown)=>part!=='body').join('.')}: ${item.msg}`).join('; '):typeof detail?.error==='string'?detail.error:typeof value?.message==='string'?value.message:response.status===401?'Для продолжения войдите в рабочее пространство.':response.status===403?'Недостаточно прав для этого действия.':`Не удалось выполнить запрос (HTTP ${response.status}).`;
    throw new ApiError(message,response.status,detail?.code||value?.code,requestId||value?.request_id,detail);
  }
  return value as T;
}
export async function upload<T=any>(path:string,files:File[],field='files',extra:Record<string,string>={}):Promise<T>{const body=new FormData();files.forEach(file=>body.append(field,file));Object.entries(extra).forEach(([key,value])=>body.append(key,value));return api<T>(path,{method:'POST',body});}
export function errorMessage(error:unknown){return error instanceof ApiError?`${error.message}${error.requestId?` Код запроса: ${error.requestId}.`:''}`:error instanceof Error?error.message:'Не удалось выполнить действие.';}
export function humanDate(value?:string|null){if(!value)return '—';const d=new Date(value);return Number.isNaN(d.getTime())?value:new Intl.DateTimeFormat('ru-RU',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit',timeZone:'Asia/Yekaterinburg'}).format(d);}
export function bytes(value:number=0){return value>=1048576?`${(value/1048576).toFixed(1)} МБ`:value>=1024?`${Math.round(value/1024)} КБ`:`${value} Б`;}
export type Source = {document_id?:string|number;solution_id?:string|number;source_type?:string;equipment_id?:string|number;title?:string;filename?:string;page?:number;pages?:number;text?:string;model?:string;score?:number;url?:string|null;excerpt?:string;original_private?:boolean;file_available?:boolean;answer_access?:'staff_only'|'client_answers'};
export type Message = {id:string|number;role:string;content:string;sources?:Source[];created_at?:string;engine?:string;attachments?:Attachment[];metadata?:Record<string,unknown>};
export function modelUnavailable(message:Message){return message.engine==='unavailable'||message.metadata?.provider_status==='unavailable'||message.metadata?.engine==='unavailable';}
export function responseMessage(response:any):Message{const value=response.message&&typeof response.message==='object'?response.message:{id:`a-${Date.now()}`,role:'assistant',content:typeof response.message==='string'?response.message:response.answer||'Ответ отсутствует.',sources:response.sources||[]};return {...value,engine:response.engine||value.engine,metadata:{...value.metadata,...(response.engine==='unavailable'?{provider_status:'unavailable',error:response.error||response.ai_error}: {})},...(response.engine==='unavailable'?{sources:[]}: {})};}
export type DocumentRecord = {id:string|number;title:string;filename:string;models:string[];category:string;visibility:string;answer_access?:'staff_only'|'client_answers';status:string;pages:number;chunks:number;size_bytes:number;created_at:string;error?:string;version?:number;indexing?:{stage:string;completed?:number;total?:number;failed_stage?:string;tables?:number;images?:number;extraction_complete?:boolean;pages_with_text?:number;unread_pages?:number[];warnings?:Array<{page?:number;code?:string;message?:string}>;index_preserved?:boolean;last_error?:string}};
export type Conversation = {id:string|number;title:string;updated_at:string};
export type User = {id:string|number;username:string;role:string};
export type Health = {status:string;mode:string;safe_mode:boolean;mail_send_enabled:boolean;documents_indexed:number;ai_configured:boolean};
export const EQUIPMENT=['PE38-12','PE38-17','PE38-25','PE53-10','AZ01','РЕ104'];

export type Attachment={id:number|string;filename:string;size_bytes?:number;mime_type?:string;url?:string;download_url?:string;status?:string;kind?:string;received?:boolean;content_processed?:boolean;processing_status?:string;analysis_kind?:string;processing_error_code?:string|null;warnings?:string[];extracted_text?:string;error?:string};
export type ConversationState={intent?:string;goal?:string;stage?:string;known_facts?:Record<string,unknown>;unconfirmed_facts?:Record<string,unknown>;missing_information?:string[];requested_action?:string;requires_human?:boolean;client_confirmed?:boolean;escalation_id?:number;task_status?:string;turn_count?:number};
export type Escalation={id:number;conversation_id:string|number;case_id?:number;task_id?:number;status:string;reason:string;summary:string;known_facts?:Record<string,unknown>;missing_information?:string[];attempted_actions?:string[];document_sources?:Source[];attachments?:Attachment[];requested_action?:string;department?:string;priority?:string;assignee?:string;created_at:string;updated_at:string;conversation?:{id:number|string;title:string;messages:Message[]};state?:ConversationState};

function isVideo(file:Attachment){
  return file.kind==='video'||/\.(?:mp4|webm|mov)$/i.test(file.filename||'');
}

export function attachmentProcessingLabel(file:Attachment){
  if(!isVideo(file))return '';
  if(file.processing_status==='missing')return 'Видео недоступно';
  if(file.processing_status==='failed')return 'Видео получено · обработка не удалась';
  if(file.processing_status==='processed'&&file.analysis_kind==='sampled_video_frames')return 'Просмотрены отдельные кадры · звук и движение не анализировались';
  return 'Видео получено · содержимое ещё не обработано';
}

export function videoProcessingHint(file:Attachment){
  if(!isVideo(file)||!['failed','missing','metadata_only'].includes(file.processing_status||''))return '';
  const warning=file.warnings?.find(value=>typeof value==='string'&&value.trim());
  if(warning)return warning;
  if(file.processing_error_code==='missing_vision_key')return 'Для анализа кадров администратору нужно настроить модель OpenAI.';
  if(file.processing_error_code==='video_probe_unavailable'||file.processing_error_code==='video_frames_unavailable'){
    return 'Для обработки видео администратору нужно настроить FFmpeg и FFprobe.';
  }
  if(file.processing_status==='missing')return 'Файл не найден в хранилище.';
  if(file.processing_status==='failed')return 'Можно прислать отдельный кадр с дисплеем или маркировкой оборудования.';
  return '';
}
