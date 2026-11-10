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
function sync_production(): array {
 if(!sync_ready())throw new RuntimeException('Production API has not been connected. Follow the private HTTPS integration instructions.');
 [$raw]=production_fetch('/v1/supervision/snapshot');
 if(strlen($raw)>12000000)throw new RuntimeException('Production response exceeded the configured safety limit');
 $payload=json_decode($raw,true,512,JSON_THROW_ON_ERROR);
 if(!is_array($payload)||!isset($payload['recordings'],$payload['contributors'],$payload['snapshot'])||!is_array($payload['recordings'])||!is_array($payload['contributors'])||!is_array($payload['snapshot'])) throw new RuntimeException('Invalid production API response schema');
 if(count($payload['recordings'])>10000||count($payload['contributors'])>10000)throw new RuntimeException('Response exceeds the configured catalog batch size');
 $agg=$payload['snapshot'];
 $total=filter_var($agg['stored']??null,FILTER_VALIDATE_INT,['options'=>['min_range'=>0]]);
 $totalRows=filter_var($agg['total_rows']??$total,FILTER_VALIDATE_INT,['options'=>['min_range'=>0]]);
 if($total===false||$totalRows===false||$total>$totalRows||$totalRows<count($payload['recordings']))throw new RuntimeException('Snapshot recording count is inconsistent');
 $acceptedMs=$agg['accepted_ms']??null;
 if($acceptedMs!==null && (!is_int($acceptedMs)||$acceptedMs<0)) throw new RuntimeException('Accepted-duration aggregate is invalid');
 if($acceptedMs!==null && $acceptedMs>($total*36000000))throw new RuntimeException('Accepted duration fails validation');
 $now=gmdate('Y-m-d H:i:s');
 $seen=[];$rows=[];
 foreach($payload['recordings'] as $rec){
  if(!is_array($rec))throw new RuntimeException('Invalid recording row');
  $id=filter_var($rec['id']??null,FILTER_VALIDATE_INT,['options'=>['min_range'=>1]]);$pid=(string)($rec['participant_id']??'');
  if($id===false||!preg_match('/^[A-Za-z0-9_-]{1,80}$/',$pid)||isset($seen[$id]))throw new RuntimeException('Invalid or duplicate recording identifier');$seen[$id]=1;
  $received=(string)($rec['received_at']??'');try{$dt=new DateTimeImmutable($received,new DateTimeZone('UTC'));}catch(Throwable){throw new RuntimeException('Invalid timestamp in production catalog');}
  $durationMs=$rec['duration_ms']??null;if($durationMs!==null && (!is_int($durationMs)||$durationMs<0||$durationMs>36000000))throw new RuntimeException('Invalid duration');
  $file=(string)($rec['file_status']??'unknown');if(!in_array($file,['stored','missing','failed','deleted','pending','unknown'],true))$file='unknown';
  $dup=$rec['duplicate_of']??null;if($dup!==null && (!is_int($dup)||$dup<1))throw new RuntimeException('Invalid duplicate marker');
  $promptType=$rec['prompt_type']??null;
  if($promptType!==null && (!is_string($promptType)||!preg_match('/^[a-z0-9_\-]{1,48}$/',$promptType)))throw new RuntimeException('Invalid prompt category in production catalog');
  $rows[]=[$id,$pid,$durationMs,$file,qc_clean((string)($rec['qc_status']??'pending')),mbcut((string)($rec['qc_reason']??''),150),$promptType,$dt->setTimezone(new DateTimeZone('UTC'))->format('Y-m-d H:i:s'),!empty($rec['is_test'])?1:0,$dup];
 }
 $persons=[];
 foreach($payload['contributors'] as $person){ if(!is_array($person))throw new RuntimeException('Invalid contributor row');$pid=(string)($person['participant_id']??'');if(!preg_match('/^[A-Za-z0-9_-]{1,80}$/',$pid))throw new RuntimeException('Invalid contributor ID');$persons[$pid]=[$pid,mbcut((string)($person['status']??'unknown'),40),!empty($person['is_test'])?'test':'real',mbcut((string)($person['batch']??''),80)]; }
 $started=gmdate('Y-m-d H:i:s');$pdo=db();$pdo->beginTransaction();
 try{
  $st=$pdo->prepare('INSERT INTO contributors(participant_id,status,contributor_type,batch) VALUES (?,?,?,?) ON DUPLICATE KEY UPDATE status=VALUES(status), contributor_type=VALUES(contributor_type),batch=VALUES(batch)');
  foreach($persons as $values)$st->execute($values);
  // Existing cached rows remain until a complete authoritative reconciliation is supported by the API.
  $st=$pdo->prepare('INSERT INTO recordings(id,participant_id,duration_ms,file_status,qc_status,qc_reason,prompt_type,received_at,is_test,duplicate_of) VALUES (?,?,?,?,?,?,?,?,?,?) ON DUPLICATE KEY UPDATE participant_id=VALUES(participant_id),duration_ms=VALUES(duration_ms),file_status=VALUES(file_status),qc_status=VALUES(qc_status),qc_reason=VALUES(qc_reason),prompt_type=VALUES(prompt_type),received_at=VALUES(received_at),is_test=VALUES(is_test),duplicate_of=VALUES(duplicate_of),imported_at=CURRENT_TIMESTAMP');
  foreach($rows as $values)$st->execute($values);
  $safe=['stored'=>(string)$total,'accepted_ms'=>$acceptedMs===null?'unknown':(string)$acceptedMs,'hours_verified'=>$acceptedMs===null?'0':'1',
  'prompt_metadata_available'=>!empty($agg['prompt_metadata_available'])?'1':'0',
  'prompt_classified'=> (string)(int)($agg['classified_recordings']??0),
  'prompt_unclassified'=> (string)(int)($agg['unclassified_recordings']??0)];
  foreach(['pending','accepted','needs_review','rejected'] as $status){$v=$agg['qc'][$status]??null;if(!is_int($v)||$v<0||$v>$totalRows)throw new RuntimeException('Invalid QC totals');$safe['qc_'.$status]=(string)$v;}
  if(array_sum(array_map('intval',array_intersect_key($safe,array_flip(['qc_pending','qc_accepted','qc_needs_review','qc_rejected']))))>$totalRows)throw new RuntimeException('QC totals exceed stored recordings');
  $safe['last_successful_sync']=$now;
  foreach($safe as $key=>$value){$st=$pdo->prepare('INSERT INTO app_settings(setting_key,setting_value) VALUES (?,?) ON DUPLICATE KEY UPDATE setting_value=VALUES(setting_value)');$st->execute([$key,$value]);}
  $st=$pdo->prepare("INSERT INTO sync_runs(status,description,started_at) VALUES ('ok',?,?)");$st->execute(['Synchronized '.count($rows).' recording rows, '.$total.' stored in production',$started]);
  $pdo->commit();return ['stored'=>$total,'rows'=>count($rows),'contributors'=>count($persons),'accepted_ms'=>$acceptedMs];
 } catch(Throwable $e){if($pdo->inTransaction())$pdo->rollBack();throw $e;}
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
   $needsSync=$previous===null||!hash_equals($previous,$hash)||!$lastFull||strtotime($lastFull)<time()-300;
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
