<?php
declare(strict_types=1);
/** Online decisions are proposed locally, and official decisions only after DigitalOcean confirms. */
function production_write_configured(): bool {
 $c=cfg();return sync_ready() && !empty($c['review_api_token']) && strlen((string)$c['review_api_token'])>=48;
}
function production_review_gateway(string $method,string $path,?array $data=null): array {
 if(!production_write_configured())throw new RuntimeException('Production review authorization is not configured');
 if(!extension_loaded('curl'))throw new RuntimeException('PHP cURL is unavailable');
 $c=cfg();$headers=['Authorization: Bearer '.$c['review_api_token'],'Accept: application/json'];
 if($data!==null)$headers[]='Content-Type: application/json';
 $ch=curl_init(production_url($path));
 curl_setopt_array($ch,[CURLOPT_RETURNTRANSFER=>true,CURLOPT_HTTPHEADER=>$headers,CURLOPT_CUSTOMREQUEST=>$method,
   CURLOPT_CONNECTTIMEOUT=>8,CURLOPT_TIMEOUT=>22,CURLOPT_FOLLOWLOCATION=>false,
   CURLOPT_SSL_VERIFYPEER=>true,CURLOPT_SSL_VERIFYHOST=>2,CURLOPT_MAXREDIRS=>0]);
 if($data!==null)curl_setopt($ch,CURLOPT_POSTFIELDS,json_encode($data,JSON_THROW_ON_ERROR));
 $body=curl_exec($ch);$status=(int)curl_getinfo($ch,CURLINFO_RESPONSE_CODE);curl_close($ch);
 if($body===false)throw new RuntimeException('Secure production review connection unavailable');
 if($status===409)throw new DomainException('Production QC changed or the recording is ineligible. Refresh the recording and inspect its current QC status.');
 if($status!==200)throw new RuntimeException('Production gateway rejected the request (HTTP '.$status.'). No official decision has been confirmed.');
 $result=json_decode($body,true,20,JSON_THROW_ON_ERROR);
 if(!is_array($result))throw new RuntimeException('Invalid gateway confirmation');
 return $result;
}
function official_write_status(): string {
 if(!production_write_configured())return 'disabled';
 try {$r=production_review_gateway('GET','/v1/supervision/write-status');return !empty($r['enabled'])?'ready':'disabled';}
 catch(Throwable){return 'unavailable';}
}
function new_operation_key(): string {
 $b=random_bytes(16);$b[6]=chr((ord($b[6])&0x0f)|0x40);$b[8]=chr((ord($b[8])&0x3f)|0x80);
 $s=bin2hex($b);return substr($s,0,8).'-'.substr($s,8,4).'-'.substr($s,12,4).'-'.substr($s,16,4).'-'.substr($s,20);
}
function deliver_review(int $draftId,array $u): string {
 $d=query('SELECT d.*,s.display_name,r.participant_id FROM review_drafts d JOIN supervisors s ON s.id=d.reviewer_id JOIN recordings r ON r.id=d.recording_id WHERE d.id=?',[$draftId])->fetch();
 if(!$d||!allowed_record((int)$d['recording_id'],$u))throw new DomainException('Review is not accessible');
 if(!in_array($d['decision'],['approve','reject'],true))throw new DomainException('This is a note, not a production QC decision');
 if($d['production_state']==='confirmed')return 'confirmed';
 if(!in_array($d['production_state'],['pending','failed'],true))throw new DomainException('This review cannot be retried; create a new decision after refreshing production status.');
 $payload=['operation_key'=>$d['operation_key'],'recording_id'=>(int)$d['recording_id'],'decision'=>$d['decision'],
    'expected_qc'=>$d['expected_qc'],'actor_id'=>(int)$d['reviewer_id'],'actor_name'=>$d['display_name'],'notes'=>$d['notes']];
 try {
  $ack=production_review_gateway('POST','/v1/supervision/reviews',$payload);
  $expect=['approve'=>'accepted','reject'=>'rejected'][$d['decision']];
  if(empty($ack['confirmed'])||!hash_equals((string)$d['operation_key'],(string)($ack['operation_key']??''))||($ack['qc_status']??'')!==$expect)
     throw new RuntimeException('Production response does not verify the expected result.');
  query("UPDATE review_drafts SET production_state='confirmed',production_qc=?,confirmed_at=UTC_TIMESTAMP(),production_message='Verified DigitalOcean confirmation' WHERE id=?",[$expect,$draftId]);
  audit('production_qc_confirmed','recording',(string)$d['recording_id'],$d['decision'].' · request '.$draftId);
  // Promptly refresh cached QC. If refresh fails, the confirmed gateway receipt is preserved.
  try {sync_production();}catch(Throwable $e){error_log('Post-QC sync failed: '.$e->getMessage());}
  return 'confirmed';
 }catch(DomainException $e){
  query("UPDATE review_drafts SET production_state='conflict',production_message=? WHERE id=?",[mbcut($e->getMessage(),250),$draftId]);
  audit('production_qc_conflict','recording',(string)$d['recording_id'],'Request '.$draftId);
  return 'conflict';
 }catch(Throwable $e){
  // A network failure could occur after remote commit. Same operation UUID makes retry idempotent.
  query("UPDATE review_drafts SET production_state='failed',production_message=? WHERE id=?",[mbcut($e->getMessage(),250),$draftId]);
  audit('production_qc_unconfirmed','recording',(string)$d['recording_id'],'Retryable request '.$draftId);
  return 'failed';
 }
}
