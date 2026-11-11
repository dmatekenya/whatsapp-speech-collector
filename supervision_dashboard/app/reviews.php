<?php
declare(strict_types=1);
/** Dunstan contract: Hostinger authenticates reviewer + assignments; DO is authoritative. */
function production_write_configured(): bool {
 $c=cfg(); return sync_ready() && !empty($c['review_api_token']) && strlen((string)$c['review_api_token'])>=48;
}
function production_review_gateway(string $method,string $path,?array $data=null): array {
 if(!production_write_configured())throw new RuntimeException('Production human-review connection is not configured');
 if(!extension_loaded('curl'))throw new RuntimeException('PHP cURL is unavailable');
 $c=cfg();$headers=['Authorization: Bearer '.$c['review_api_token'],'Accept: application/json'];
 if($data!==null)$headers[]='Content-Type: application/json';
 $ch=curl_init(production_url($path));
 curl_setopt_array($ch,[CURLOPT_RETURNTRANSFER=>true,CURLOPT_HTTPHEADER=>$headers,CURLOPT_CUSTOMREQUEST=>$method,
   CURLOPT_CONNECTTIMEOUT=>8,CURLOPT_TIMEOUT=>22,CURLOPT_FOLLOWLOCATION=>false,
   CURLOPT_SSL_VERIFYPEER=>true,CURLOPT_SSL_VERIFYHOST=>2,CURLOPT_MAXREDIRS=>0]);
 if($data!==null)curl_setopt($ch,CURLOPT_POSTFIELDS,json_encode($data,JSON_THROW_ON_ERROR));
 $body=curl_exec($ch);$status=(int)curl_getinfo($ch,CURLINFO_RESPONSE_CODE);curl_close($ch);
 if($body===false)throw new RuntimeException('Production review connection unavailable');
 if($status===409)throw new DomainException('The Tier 3 decision changed or this request conflicts. Refresh before retrying.');
 if($status!==200)throw new RuntimeException('Production review not confirmed (HTTP '.$status.').');
 $result=json_decode($body,true,20,JSON_THROW_ON_ERROR);
 if(!is_array($result))throw new RuntimeException('Invalid production confirmation');
 return $result;
}
function official_write_status(): string {
 if(!production_write_configured())return 'disabled';
 try{$r=production_review_gateway('GET','/v1/supervision/write-status');
  return !empty($r['enabled'])&&($r['mode']??'')==='human-review'&&($r['decisions']??null)===['accept','reject']&&($r['collector_qc_mutation']??true)===false?'ready':'disabled';
 }catch(Throwable){return 'unavailable';}
}
function new_operation_key(): string {
 $b=random_bytes(16);$b[6]=chr((ord($b[6])&0x0f)|0x40);$b[8]=chr((ord($b[8])&0x3f)|0x80);
 $s=bin2hex($b);return substr($s,0,8).'-'.substr($s,8,4).'-'.substr($s,12,4).'-'.substr($s,16,4).'-'.substr($s,20);
}
function deliver_review(int $draftId,array $u): string {
 // A saved draft is NOT a production decision; never label it confirmed locally.
 $d=query('SELECT d.*,s.display_name,r.participant_id FROM review_drafts d JOIN supervisors s ON s.id=d.reviewer_id JOIN recordings r ON r.id=d.recording_id WHERE d.id=?',[$draftId])->fetch();
 if(!$d || !allowed_record((int)$d['recording_id'],$u))throw new DomainException('Review not accessible');
 if($u['role']!=='admin' && (int)$d['reviewer_id']!==(int)$u['id'])throw new DomainException('Review does not belong to this supervisor');
 if(!in_array($d['decision'],['accept','reject'],true))throw new DomainException('This entry is not a Tier 3 decision');
 if($d['production_state']==='confirmed')return 'confirmed';
 if(!in_array($d['production_state'],['pending','failed'],true))throw new DomainException('Conflict must be resolved by a new decision');
 $payload=['operation_key'=>(string)$d['operation_key'],'recording_id'=>(int)$d['recording_id'],
    'contributor_id'=>(string)$d['participant_id'],'decision'=>(string)$d['decision'],
    'reason_code'=>$d['reason_code']?:null,'note'=>$d['notes']?:null,
    'reviewer_id'=>(string)$d['reviewer_id'],
    'reviewer_name'=>(string)($d['reviewer_name_snapshot']?:$d['display_name']),
    'expected_qc_tier3'=>$d['expected_tier3']?:null];
 try{
  $ack=production_review_gateway('POST','/v1/supervision/reviews',$payload);
  if(empty($ack['confirmed'])||($ack['mode']??'')!=='human-review'||
     !hash_equals((string)$d['operation_key'],(string)($ack['operation_key']??''))||
     ($ack['human_decision']??'')!==$d['decision'])
     throw new RuntimeException('Production response cannot confirm this decision');
  query("UPDATE review_drafts SET production_state='confirmed',production_qc=?,confirmed_at=UTC_TIMESTAMP(),production_message='Human review saved to DigitalOcean' WHERE id=?",[$d['decision'],$draftId]);
  audit('human_review_confirmed','recording',(string)$d['recording_id'],$d['decision'].' · request '.$draftId);
  try{sync_production();}catch(Throwable $e){error_log('Post-review sync failed: '.$e->getMessage());}
  return 'confirmed';
 }catch(DomainException $e){
  query("UPDATE review_drafts SET production_state='conflict',production_message=? WHERE id=?",[mbcut($e->getMessage(),250),$draftId]);
  audit('review_conflict','recording',(string)$d['recording_id'],'Request '.$draftId);
  return 'conflict';
 }catch(Throwable $e){
  // Network may fail AFTER DO commits. Same UUID makes replays idempotent.
  query("UPDATE review_drafts SET production_state='failed',production_message=? WHERE id=?",[mbcut($e->getMessage(),250),$draftId]);
  audit('review_unconfirmed','recording',(string)$d['recording_id'],'Request '.$draftId);
  return 'failed';
 }
}
