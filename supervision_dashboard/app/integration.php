<?php
declare(strict_types=1);
/** Authorized production connector. DigitalOcean needs a separate HTTPS endpoint approved by its operator. */
function production_url(string $path): string { $c=cfg();$base=rtrim((string)($c['api_url']??''),'/');if(!preg_match('~^https://[A-Za-z0-9._:-]+(?:/[^?#]*)?$~',$base))throw new RuntimeException('Production HTTPS API is not configured');return $base.$path; }
function production_fetch(string $path,?string $range=null): array {
 if(!extension_loaded('curl'))throw new RuntimeException('PHP cURL extension is unavailable');
 $c=cfg(); if(empty($c['api_token'])||strlen($c['api_token'])<24)throw new RuntimeException('Production API credential is not configured');
 $headers=['Authorization: Bearer '.$c['api_token'],str_contains($path,'/audio/')?'Accept: audio/*':'Accept: application/json'];
 $ch=curl_init(production_url($path));
 curl_setopt_array($ch,[CURLOPT_RETURNTRANSFER=>true,CURLOPT_HTTPHEADER=>$headers,CURLOPT_FOLLOWLOCATION=>false,CURLOPT_CONNECTTIMEOUT=>8,CURLOPT_TIMEOUT=>(int)($c['api_timeout']??25),CURLOPT_SSL_VERIFYPEER=>true,CURLOPT_SSL_VERIFYHOST=>2,CURLOPT_USERAGENT=>'Masakhane-Supervision/0.0.0.1',CURLOPT_MAXREDIRS=>0]);
 $body=curl_exec($ch);$error=curl_error($ch);$code=(int)curl_getinfo($ch,CURLINFO_RESPONSE_CODE);$mime=(string)curl_getinfo($ch,CURLINFO_CONTENT_TYPE);curl_close($ch);
 if($body===false)throw new RuntimeException('Production connection failed: '.$error);
 if($code!==200 && $code!==206)throw new RuntimeException('Production API responded with HTTP '.$code);
 return [$body,$code,$mime];
}
function qc_clean(string $value): string {return in_array(strtolower($value),['accepted','pending','needs_review','rejected'],true)?strtolower($value):'pending';}
/** Dunstan contract: bounded keyset sync using recordings.updated_at; no catalogue cap.
 * Checkpoint every page in the SAME MySQL transaction as its recording upserts.
 * A fixed upper watermark avoids skipping concurrent source updates.
 */
function sync_production(): array {
 if(!sync_ready())throw new RuntimeException('Production gateway not configured');
 $lock=(int)db()->query("SELECT GET_LOCK('masakhane_tier3_metadata_cursor',0)")->fetchColumn();
 if($lock!==1)throw new RuntimeException('A production synchronization is already running');
 try {
 $after=setting('tier3_cursor_after','1970-01-01T00:00:00Z');
 $afterId=(int)setting('tier3_cursor_id','0');
 $until=setting('tier3_sync_until');
 $imported=0;$partial=false;$contributorsSeen=[];
 $started=gmdate('Y-m-d H:i:s');
 // Limited time per invocation; continues from stored cursor on the next cron/probe.
 for($page=0;$page<8;$page++){
  $url='/v1/supervision/changes?'.http_build_query(['after'=>$after,'after_id'=>$afterId,'limit'=>500]+($until?['until'=>$until]:[]),'','&',PHP_QUERY_RFC3986);
  [$raw]=production_fetch($url);
  if(strlen($raw)>1800000)throw new RuntimeException('Incremental page exceeds safety budget');
  $payload=json_decode($raw,true,256,JSON_THROW_ON_ERROR);
  if(!is_array($payload)||!is_array($payload['recordings']??null)||!is_array($payload['contributors']??null)||!is_array($payload['cursor']??null)||!isset($payload['until'])||!is_bool($payload['has_more']??null))throw new RuntimeException('Invalid production changes payload');
  if(count($payload['recordings'])>500||count($payload['contributors'])>500)throw new RuntimeException('Incremental page too large');
  if($until!==null && !hash_equals($until,(string)$payload['until']))throw new RuntimeException('Production synchronization watermark changed');
  $until=(string)$payload['until'];
  $cursor=(string)($payload['cursor']['after']??'');$cursorId=$payload['cursor']['after_id']??null;
  if(!preg_match('/^\d{4}-\d{2}-\d{2}T/', $cursor) || !is_int($cursorId)||$cursorId<0)throw new RuntimeException('Invalid production cursor');
  $rows=[];$seen=[];
  foreach($payload['recordings'] as $rec){
   if(!is_array($rec))throw new RuntimeException('Invalid production recording');
   $id=filter_var($rec['id']??null,FILTER_VALIDATE_INT,['options'=>['min_range'=>1]]);$pid=(string)($rec['participant_id']??'');
   if($id===false||!preg_match('/^[A-Za-z0-9_-]{1,80}$/D',$pid)||isset($seen[$id]))throw new RuntimeException('Invalid or duplicate recording ID');$seen[$id]=true;
   $created=$rec['received_at']??null;$updated=$rec['updated_at']??null;
   try{$received=(new DateTimeImmutable((string)$created))->setTimezone(new DateTimeZone('UTC'))->format('Y-m-d H:i:s');
       $updatedDb=(new DateTimeImmutable((string)$updated))->setTimezone(new DateTimeZone('UTC'))->format('Y-m-d H:i:s.u');}
   catch(Throwable){throw new RuntimeException('Invalid production timestamp');}
   $ms=$rec['duration_ms']??null;
   if($ms!==null&&(!is_int($ms)||$ms<0||$ms>36000000))throw new RuntimeException('Invalid recorded duration');
   $file=(string)($rec['file_status']??'unknown');
   if(!in_array($file,['stored','missing','failed','deleted','pending','unknown'],true))$file='unknown';
   $dup=$rec['duplicate_of']??null;if($dup!==null&&(!is_int($dup)||$dup<1))throw new RuntimeException('Invalid duplicate ID');
   $prompt=$rec['prompt_type']??null;
   if($prompt!==null&&(!is_string($prompt)||!preg_match('/^[a-z0-9_-]{1,48}$/D',$prompt)))throw new RuntimeException('Invalid prompt type');
   $t2=$rec['qc_tier2']??null;
   if($t2!==null&&(!is_int($t2)||$t2<1||$t2>4))throw new RuntimeException('Invalid Tier 2 score');
   $t3=$rec['qc_tier3']??null;
   if($t3!==null&&!in_array($t3,['accept','reject'],true))throw new RuntimeException('Invalid Tier 3 decision');
   $final=$rec['qc_final']??null;
   if($final!==null&&!in_array($final,['bronze','silver','gold','rejected'],true))throw new RuntimeException('Invalid final QC label');
   $human=$rec['human_decision']??null;
   if($human!==null&&!in_array($human,['accept','reject'],true))throw new RuntimeException('Invalid human-review history');
   $key=$rec['human_operation_key']??null;
   if($key!==null && (!is_string($key)||!preg_match('/^[a-f0-9-]{36}$/D',$key)))throw new RuntimeException('Invalid human review key');
   $humanAt=null;
   if($rec['human_reviewed_at']??null){try{$humanAt=(new DateTimeImmutable($rec['human_reviewed_at']))->setTimezone(new DateTimeZone('UTC'))->format('Y-m-d H:i:s');}catch(Throwable){throw new RuntimeException('Invalid review timestamp');}}
   $rows[]=[$id,$pid,$ms,$file,qc_clean((string)($rec['qc_status']??'')),mbcut((string)($rec['qc_reason']??''),150),$prompt,$received,!empty($rec['is_test'])?1:0,$dup,$human,$key,$humanAt,
     $t2,$t3,mbcut((string)($rec['qc_tier3_reason']??''),80),$final,mbcut((string)($rec['band_reason']??''),200),$updatedDb];
  }
  $people=[];
  foreach($payload['contributors'] as $p){
   if(!is_array($p)||!preg_match('/^[A-Za-z0-9_-]{1,80}$/D',(string)($p['participant_id']??'')))throw new RuntimeException('Invalid contributor metadata');
   $pid=(string)$p['participant_id'];$contributorsSeen[$pid]=1;
   $people[$pid]=[$pid,mbcut((string)($p['status']??'unknown'),40),!empty($p['is_test'])?'test':'real',mbcut((string)($p['batch']??''),80)];
  }
  $pdo=db();$pdo->beginTransaction();
  try {
   $addPerson=$pdo->prepare('INSERT INTO contributors(participant_id,status,contributor_type,batch) VALUES (?,?,?,?) ON DUPLICATE KEY UPDATE status=VALUES(status),contributor_type=VALUES(contributor_type),batch=VALUES(batch)');
   foreach($people as $p)$addPerson->execute($p);
   $sql='INSERT INTO recordings(id,participant_id,duration_ms,file_status,qc_status,qc_reason,prompt_type,received_at,is_test,duplicate_of,human_decision,human_operation_key,human_reviewed_at,qc_tier2,qc_tier3,qc_tier3_reason,qc_final,band_reason,source_updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON DUPLICATE KEY UPDATE participant_id=VALUES(participant_id),duration_ms=VALUES(duration_ms),file_status=VALUES(file_status),qc_status=VALUES(qc_status),qc_reason=VALUES(qc_reason),prompt_type=VALUES(prompt_type),received_at=VALUES(received_at),is_test=VALUES(is_test),duplicate_of=VALUES(duplicate_of),human_decision=VALUES(human_decision),human_operation_key=VALUES(human_operation_key),human_reviewed_at=VALUES(human_reviewed_at),qc_tier2=VALUES(qc_tier2),qc_tier3=VALUES(qc_tier3),qc_tier3_reason=VALUES(qc_tier3_reason),qc_final=VALUES(qc_final),band_reason=VALUES(band_reason),source_updated_at=VALUES(source_updated_at),imported_at=CURRENT_TIMESTAMP';
   $upsert=$pdo->prepare($sql);
   $receipt=$pdo->prepare("UPDATE review_drafts SET production_state='confirmed',production_qc=?,confirmed_at=?,production_message='Verified by the DigitalOcean append-only audit' WHERE recording_id=? AND operation_key=? AND decision=? AND production_state IN ('pending','failed','conflict')");
   foreach($rows as $r){$upsert->execute($r);if($r[10]!==null&&$r[11]!==null)$receipt->execute([$r[10],$r[12],$r[0],$r[11],$r[10]]);}
   set_setting('tier3_cursor_after',$cursor);set_setting('tier3_cursor_id',(string)$cursorId);
   set_setting('tier3_sync_until',$payload['has_more']?$until:'');
   set_setting('last_successful_sync',gmdate('Y-m-d H:i:s'));
   $pdo->commit();
  }catch(Throwable $e){if($pdo->inTransaction())$pdo->rollBack();throw $e;}
  $imported+=count($rows);$after=$cursor;$afterId=$cursorId;
  $partial=$payload['has_more'];if(!$partial)break;
 }
 // Aggregate is calculated by DigitalOcean, not by summing sparse incremental pages.
 try {
  [$raw]=production_fetch('/v1/supervision/summary');
  if(strlen($raw)>8192)throw new RuntimeException('Quality summary too large');
  $sum=json_decode($raw,true,32,JSON_THROW_ON_ERROR);
  foreach(['stored','total_rows','collected_ms','qc_final_available','final_counts'] as $k)if(!array_key_exists($k,$sum))throw new RuntimeException('Invalid quality summary');
  if(!is_bool($sum['qc_final_available'])||!is_int($sum['collected_ms'])||$sum['collected_ms']<0||!is_int($sum['stored'])||$sum['stored']<0)throw new RuntimeException('Invalid quality aggregates');
  $settings=['stored'=>(string)$sum['stored'],'collected_ms'=>(string)$sum['collected_ms'],
    'qc_final_available'=>$sum['qc_final_available']?'1':'0',
    'usable_ms'=>$sum['qc_final_available']?(string)$sum['usable_ms']:'unknown',
    'bronze_ms'=>$sum['qc_final_available']?(string)$sum['bronze_ms']:'unknown',
    'rejected_final_ms'=>$sum['qc_final_available']?(string)$sum['rejected_ms']:'unknown',
    'hours_verified'=>$sum['qc_final_available']?'1':'0',
    'accepted_ms'=>$sum['qc_final_available']?(string)$sum['usable_ms']:'unknown'];
  // QC grants can arrive AFTER the pilot's initial read-only sync. Once
  // final QC becomes readable, replay the catalog so older clips gain labels.
  $becameAvailable=($sum['qc_final_available'] && setting('qc_final_available','0')!=='1');
  foreach($settings as $k=>$v)set_setting($k,$v);
  if($becameAvailable){
    set_setting('tier3_cursor_after','1970-01-01T00:00:00Z');
    set_setting('tier3_cursor_id','0');
    set_setting('tier3_sync_until','');
  }
 }catch(Throwable $e){error_log('DigitalOcean quality summary unavailable: '.$e->getMessage());}
 query('INSERT INTO sync_runs(status,description,started_at) VALUES (\'ok\',?,?)',[(string)('Imported '.$imported.' changed recordings'.($partial?' (more pending)':'')),$started]);
 return ['stored'=>(int)setting('stored','0'),'rows'=>$imported,'contributors'=>count($contributorsSeen),
    'accepted_ms'=>setting('usable_ms'),'more'=>$partial];
 }finally{db()->query("SELECT RELEASE_LOCK('masakhane_tier3_metadata_cursor')");}
}
function sync_failed(string $detail): void {try{query("INSERT INTO sync_runs(status,description,started_at) VALUES ('failed',?,UTC_TIMESTAMP())",[mbcut($detail,490)]);}catch(Throwable $ignored){} }
/** Backend-coordinated 5-second change checks while authenticated pages are open. */
function live_sync_state(array $u): array {
 [$scope,$params]=scope_sql($u);
 $n=count_value('SELECT COUNT(*) FROM recordings r WHERE r.id > COALESCE((SELECT last_seen_recording_id FROM notification_state WHERE user_id=?),0)'.$scope,array_merge([(int)$u['id']],$params));
 return ['unread'=>$n,'last_sync'=>sync_timestamp(),'stored'=>(int)setting('stored','0'),
         'last_probe'=>setting('live_last_probe'), 'ready'=>sync_ready(),
         'revision'=>setting('live_revision_hash',''),
         'last_sync_ml'=>sync_timestamp()?date_ml(sync_timestamp()):null];
}
function live_refresh(array $u): array {
 $result=live_sync_state($u); $result['changed']=false; $result['checked']=false;
 if(!sync_ready()){$result['error']='Production HTTPS gateway is not configured';return $result;}
 $pdo=db();
 $lock=(int)$pdo->query("SELECT GET_LOCK('masakhane_live_sync_v001',0)")->fetchColumn();
 if($lock!==1){$result['busy']=true;return $result;}
 try {
   $last=setting('live_last_probe');
   if($last && strtotime($last)>time()-5)return live_sync_state($u)+['changed'=>false,'checked'=>false];
   // Mark probe before outgoing HTTP call to avoid stampedes across requests.
   set_setting('live_last_probe',gmdate('Y-m-d H:i:s'));
   [$raw]=production_fetch('/v1/supervision/revision');
   if(strlen($raw)>4096)throw new RuntimeException('Revision response too large');
   $rev=json_decode($raw,true,32,JSON_THROW_ON_ERROR);
   if(!is_array($rev)||!isset($rev['recordings'],$rev['newest_id'],$rev['latest_update'],$rev['participant_digest']))throw new RuntimeException('Invalid revision response');
   $hash=hash('sha256',json_encode($rev,JSON_THROW_ON_ERROR));
   $previous=setting('live_revision_hash');
   $lastFull=sync_timestamp();
   $needsSync=$previous===null||!hash_equals($previous,$hash)||!$lastFull||strtotime($lastFull)<time()-300||((string)setting('tier3_sync_until','')!=='');
   if($needsSync){
      sync_production();
      set_setting('live_revision_hash',$hash);
      $result['changed']=($previous===null || !hash_equals($previous,$hash));
   }
   $result=array_merge($result,live_sync_state($u));$result['checked']=true;
 } catch(Throwable $e){
   error_log('Supervision live refresh: '.get_class($e).': '.$e->getMessage());
   $result['error']='Production update check unavailable';
 } finally {
   $pdo->query("SELECT RELEASE_LOCK('masakhane_live_sync_v001')");
 }
 return $result;
}
function audio_status_response(int $id,array $user): never {
 header('Content-Type: application/json; charset=utf-8');header('Cache-Control: private, no-store');
 if(!allowed_record($id,$user)){http_response_code(404);echo json_encode(['available'=>false,'message'=>'Recording not accessible']);exit;}
 if(!sync_ready()){http_response_code(503);echo json_encode(['available'=>false,'message'=>'Secure production gateway unavailable']);exit;}
 try{
   [$body]=production_fetch('/v1/supervision/audio/'.$id.'/status');
   $obj=json_decode($body,true,16,JSON_THROW_ON_ERROR);
   if(empty($obj['available'])||!in_array($obj['mime']??'', ['audio/mpeg','audio/ogg','audio/wav','audio/webm','audio/mp4','audio/aac','audio/amr'],true))throw new RuntimeException('Audio status invalid');
   echo json_encode(['available'=>true,'mime'=>$obj['mime'],'bytes'=>(int)($obj['bytes']??0)]);exit;
 }catch(Throwable $e){
   error_log('Supervision playback preflight recording #'.$id.': '.$e->getMessage());
   http_response_code(503);echo json_encode(['available'=>false,'message'=>'Audio is not available from production. The server administrator should check the audio gateway diagnostics.']);exit;
 }
}
function audio_response(int $id,array $user): never {
 if(!allowed_record($id,$user)){http_response_code(404);exit('Recording unavailable');}
 if(!sync_ready()){http_response_code(503);header('Content-Type: text/plain');exit('Production audio service not configured');}
 try {
  [$body,$code,$mime]=production_fetch('/v1/supervision/audio/'.$id);
  if(strlen($body)>25000000||strlen($body)<1)throw new RuntimeException('Audio size invalid');
  $type=strtolower(trim(explode(';',$mime)[0]));
  if(!in_array($type,['audio/mpeg','audio/ogg','audio/wav','audio/webm','audio/mp4','audio/aac','audio/amr'],true))throw new RuntimeException('Unexpected audio MIME: '.$type);
  $size=strlen($body);$from=0;$to=$size-1;$partial=false;
  $range=$_SERVER['HTTP_RANGE']??'';
  if($range!==''){
   if(!preg_match('/^bytes=(\d*)-(\d*)$/',$range,$matches)||($matches[1]===''&&$matches[2]==='')){http_response_code(416);header('Content-Range: bytes */'.$size);exit;}
   if($matches[1]===''){ $n=(int)$matches[2];$from=max(0,$size-$n);}
   else{ $from=(int)$matches[1];if($matches[2]!=='')$to=min($size-1,(int)$matches[2]); }
   if($from>$to||$from>=$size){http_response_code(416);header('Content-Range: bytes */'.$size);exit;}
   $partial=true;
  }
  audit('audio_play','recording',(string)$id,'Authorized HTTPS playback');
  http_response_code($partial?206:200);
  header('Content-Type: '.$type);header('X-Content-Type-Options: nosniff');
  header('Cache-Control: private, no-store, max-age=0');header('Accept-Ranges: bytes');
  if($partial)header('Content-Range: bytes '.$from.'-'.$to.'/'.$size);
  header('Content-Length: '.($to-$from+1));
  echo ($partial?substr($body,$from,$to-$from+1):$body);exit;
 }catch(Throwable $e){error_log('Production audio #'.$id.': '.$e->getMessage());http_response_code(503);header('Content-Type: text/plain');exit('Audio unavailable. Contact the administrator to check the production audio gateway.');}
}
