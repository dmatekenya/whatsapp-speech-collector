<?php
declare(strict_types=1);
function post_action(string $page,array $u): never {
 check_csrf();
 switch($page){
 case 'sync':
  require_admin();
  if(!sync_ready()){notify('warning','Production HTTPS gateway is not connected yet. No data was imported. Open Integration status to complete the secure setup.');redirect('integration');}
  if(!isset($_SESSION['last_manual_sync'])||time()-(int)$_SESSION['last_manual_sync']>=45){
   $_SESSION['last_manual_sync']=time();try{$r=sync_production();audit('production_sync','system',null,'Stored '.$r['stored'].'; imported '.$r['rows']);notify('success','Production metadata synchronized successfully: '.$r['stored'].' recordings.');}catch(Throwable $e){error_log('Manual sync error: '.$e->getMessage());sync_failed('Secure production connection failed');notify('error','Synchronization failed. No new data was imported. Check the secure production connection and sync log.');}
  }else notify('warning','Sync is rate-limited to once every 45 seconds.');redirect('integration');
 case 'save_review':
  $id=(int)($_POST['recording_id']??0);
  if(!allowed_record($id,$u)){http_response_code(403);exit('Recording not accessible');}
  $decision=(string)($_POST['decision']??'flag');
  if(!in_array($decision,['flag','accept','reject'],true)){notify('error','Invalid review decision.');redirect('voice',['id'=>$id]);}
  if(empty($_POST['listened'])){notify('error','Confirm that you listened to this recording.');redirect('voice',['id'=>$id]);}
  $notes=trim((string)($_POST['notes']??''));
  $reason=(string)($_POST['reason_code']??'');
  $reasons=['wrong_sentence','no_speech','noise','not_own_voice','unclear','not_chichewa','other'];
  if(strlen($notes)>500||($decision==='flag'&&strlen($notes)<5)){
   notify('error','Online notes must contain 5–500 characters; optional QC comments must be at most 500.');redirect('voice',['id'=>$id]);
  }
  if($decision==='reject'&&(!in_array($reason,$reasons,true)||($reason==='other'&&strlen($notes)<5))){
   notify('error','Select a rejection reason; describe an Other reason in your note.');redirect('voice',['id'=>$id]);
  }
  if($decision!=='reject')$reason=null;
  $record=query('SELECT qc_status,qc_tier3,file_status,is_test,duplicate_of FROM recordings WHERE id=?',[$id])->fetch();
  if(!$record||$record['file_status']!=='stored'){
    notify('error','Recording is not stored.');redirect('voice',['id'=>$id]);
  }
  if($decision!=='flag' && official_write_status()!=='ready'){
   notify('warning','Dunstan has disabled production QC write-back. Notes can still be saved; no official decision was sent.');redirect('voice',['id'=>$id]);
  }
  if($decision!=='flag' && $record['qc_tier3']===$decision){
   notify('warning','This Tier 3 decision is already current.');redirect('voice',['id'=>$id]);
  }
  if($decision!=='flag' && $record['qc_tier3']!==null && empty($_POST['reversal_confirm'])){
   notify('error','A reversal requires explicit confirmation.');redirect('voice',['id'=>$id]);
  }
  if($decision==='accept'&&((int)$record['is_test']===1||$record['duplicate_of']!==null)){
   notify('error','Test and duplicate recordings cannot be accepted.');redirect('voice',['id'=>$id]);
  }
  $operation=$decision==='flag'?null:new_operation_key();
  $initial=$decision==='flag'?'draft':'pending';
  try{
   query('INSERT INTO review_drafts(recording_id,reviewer_id,reviewer_name_snapshot,decision,notes,listened,production_state,operation_key,expected_qc,reason_code,expected_tier3) VALUES (?,?,?,?,?,1,?,?,?,?,?)',
     [$id,$u['id'],$u['display_name'],$decision,$notes,$initial,$operation,$record['qc_status'],$reason,$record['qc_tier3']]);
   $draftId=(int)db()->lastInsertId();audit('review_created','recording',(string)$id,$decision.' · request '.$draftId);
  }catch(Throwable $e){error_log('Review save: '.$e->getMessage());notify('error','Review could not be saved.');redirect('voice',['id'=>$id]);}
  if($decision==='flag'){
   notify('success','Online note saved. No DigitalOcean QC decision was changed.');redirect('voice',['id'=>$id]);
  }
  $state=deliver_review($draftId,$u);
  if($state==='confirmed'){
   notify('success','Recording #'.$id.' '.($decision==='accept'?'accepted':'rejected').' and confirmed by DigitalOcean.');
   $next=next_eligible_review_id($u,$id);
   if($next!==null)redirect('voice',['id'=>$next]);
   notify('success','No additional pending recordings in your assigned queue.');redirect('queue');
  }
  elseif($state==='conflict')notify('error','Tier 3 state changed. Refresh the record and review before resubmitting.');
  else notify('warning','Review not confirmed by DigitalOcean. Retry with the same saved operation key.');
  redirect('voice',['id'=>$id]);
 case 'retry_review':
  $draftId=(int)($_POST['review_id']??0);
  $d=query('SELECT recording_id,reviewer_id,production_state FROM review_drafts WHERE id=?',[$draftId])->fetch();
  if(!$d || (int)$d['reviewer_id']!==(int)$u['id'] && $u['role']!=='admin' || !allowed_record((int)$d['recording_id'],$u)){
   http_response_code(403);exit('Review not accessible');
  }
  if($d['production_state']!=='failed' && $d['production_state']!=='pending'){
    notify('warning','Only unconfirmed submissions can be retried.');redirect('voice',['id'=>$d['recording_id']]);
  }
  $state=deliver_review($draftId,$u);
  notify($state==='confirmed'?'success':($state==='conflict'?'error':'warning'),
    $state==='confirmed'?'Supervisor decision confirmed by DigitalOcean.':'Delivery unsuccessful; no production QC change is recorded.');
  redirect('voice',['id'=>$d['recording_id']]);
 case 'mark_seen':
  [$seenScope,$seenArgs]=scope_sql($u);$highest=(int)query('SELECT COALESCE(MAX(r.id),0) FROM recordings r WHERE 1=1'.$seenScope,$seenArgs)->fetchColumn();
  query('INSERT INTO notification_state(user_id,last_seen_recording_id) VALUES (?,?) ON DUPLICATE KEY UPDATE last_seen_recording_id=GREATEST(last_seen_recording_id,VALUES(last_seen_recording_id))',[$u['id'],$highest]);audit('notifications_seen','recordings',(string)$highest);notify('success','Available recordings marked as seen for your account.');redirect('notifications');
 case 'create_supervisor':
  require_admin();$email=trim((string)($_POST['email']??''));$name=trim((string)($_POST['name']??''));$pass=(string)($_POST['password']??'');
  if(!filter_var($email,FILTER_VALIDATE_EMAIL)||strlen($name)<2||strlen($name)>120||strlen($pass)<14){notify('error','Enter a valid name, email and password of at least 14 characters.');redirect('supervisors');}
  try{query("INSERT INTO supervisors(display_name,email,password_hash,role) VALUES (?,?,?,'supervisor')",[$name,$email,password_hash($pass,defined('PASSWORD_ARGON2ID')?PASSWORD_ARGON2ID:PASSWORD_DEFAULT)]);audit('supervisor_created','email',$email);notify('success','Supervisor account created. Assign permitted contributor IDs before use.');}catch(Throwable){notify('error','Could not create this supervisor. Check whether their email is already registered.');}
  redirect('supervisors');
 case 'assignment_batch':
  require_admin();$supervisorId=(int)($_POST['user']??0);
  if(!query("SELECT 1 FROM supervisors WHERE id=? AND role='supervisor' AND active=1",[$supervisorId])->fetchColumn()){
    notify('error','Choose an active supervisor.');redirect('supervisors');
  }
  $ids=$_POST['contributors']??[];
  if(!is_array($ids)||!$ids||count($ids)>150){notify('error','Select between 1 and 150 contributors.');redirect('assignments',['user'=>$supervisorId]);}
  $ids=array_values(array_unique(array_map('strval',$ids)));
  foreach($ids as $pid)if(!preg_match('/^[A-Za-z0-9_-]{1,80}$/',$pid)){
    notify('error','The selection contains an invalid contributor.');redirect('assignments',['user'=>$supervisorId]);
  }
  sort($ids,SORT_STRING);
  $pdo=db();$pdo->beginTransaction();
  try {
   $holders=implode(',',array_fill(0,count($ids),'?'));
   $st=query("SELECT participant_id FROM contributors WHERE participant_id IN ($holders) ORDER BY participant_id FOR UPDATE",$ids);
   $available=$st->fetchAll(PDO::FETCH_COLUMN);
   if(count($available)!==count($ids))throw new DomainException('One or more contributor IDs are not synchronized');
   $remove=$pdo->prepare('DELETE FROM supervisor_assignments WHERE participant_id=?');
   $insert=$pdo->prepare('INSERT INTO supervisor_assignments(supervisor_id,participant_id) VALUES (?,?)');
   foreach($ids as $pid){$remove->execute([$pid]);$insert->execute([$supervisorId,$pid]);}
   $pdo->commit();audit('assignment_bulk','supervisor',(string)$supervisorId,count($ids).' contributors assigned exclusively');
   notify('success',count($ids).' contributors assigned. Existing supervisor assignments for these IDs were replaced.');
  }catch(Throwable $e){if($pdo->inTransaction())$pdo->rollBack();error_log('Assignment batch: '.$e->getMessage());notify('error','Assignment was not completed. No partial reassignment was saved.');}
  redirect('assignments',['user'=>$supervisorId]);
 case 'assignment_add': case 'assignment_remove':
  require_admin();$id=(int)($_POST['user']??0);$pid=trim((string)($_POST['participant_id']??''));
  if(!preg_match('/^[A-Za-z0-9_-]{1,80}$/',$pid)||!query("SELECT 1 FROM supervisors WHERE id=? AND role='supervisor'",[$id])->fetchColumn()){notify('error','Invalid assignment request.');redirect('supervisors');}
  if($page==='assignment_add'){
    if(!query('SELECT 1 FROM contributors WHERE participant_id=?',[$pid])->fetchColumn()){notify('error','This contributor ID is not in the synchronized catalog.');redirect('assignments',['user'=>$id]);}
    $pdo=db();$pdo->beginTransaction();try{query('SELECT participant_id FROM contributors WHERE participant_id=? FOR UPDATE',[$pid]);query('DELETE FROM supervisor_assignments WHERE participant_id=?',[$pid]);query('INSERT INTO supervisor_assignments(supervisor_id,participant_id) VALUES (?,?)',[$id,$pid]);$pdo->commit();}catch(Throwable $e){if($pdo->inTransaction())$pdo->rollBack();throw $e;}
  }else query('DELETE FROM supervisor_assignments WHERE supervisor_id=? AND participant_id=?',[$id,$pid]);
  audit($page,'contributor',$pid,'Supervisor #'.$id);notify('success','Contributor assignment updated.');redirect('assignments',['user'=>$id]);
 case 'toggle_supervisor':
  require_admin();$id=(int)($_POST['user']??0);query("UPDATE supervisors SET active=1-active WHERE id=? AND role='supervisor'",[$id]);audit('supervisor_active_toggle','account',(string)$id);redirect('assignments',['user'=>$id]);
 case 'save_settings':
  require_admin();$n=(int)($_POST['refresh_seconds']??5);
  if(!in_array($n,[5,10,15,30,60],true)){notify('error','Choose a valid refresh interval.');redirect('settings');}
  set_setting('refresh_seconds',(string)$n);audit('settings_change','refresh_seconds',(string)$n);notify('success','Online settings saved for the dashboard.');redirect('settings');
 case 'update_profile':
  $name=trim((string)($_POST['name']??''));
  if(strlen($name)<2||strlen($name)>120){notify('error','Display name must contain 2–120 characters.');redirect('profile');}
  query('UPDATE supervisors SET display_name=? WHERE id=?',[$name,$u['id']]);
  audit('profile_update','supervisor',(string)$u['id']);notify('success','Profile updated successfully.');redirect('profile');
 case 'change_password':
  $cur=(string)($_POST['current_password']??'');$new=(string)($_POST['new_password']??'');$confirm=(string)($_POST['confirm_password']??'');
  $hash=(string)query('SELECT password_hash FROM supervisors WHERE id=?',[$u['id']])->fetchColumn();
  if(!password_verify($cur,$hash)){notify('error','Current password is incorrect. No changes were made.');redirect('profile');}
  if(strlen($new)<12||strlen($new)>128||!hash_equals($new,$confirm)||password_verify($new,$hash)){
   notify('error','Use a different password of 12–128 characters and confirm it correctly.');redirect('profile');
  }
  $newHash=password_hash($new,defined('PASSWORD_ARGON2ID')?PASSWORD_ARGON2ID:PASSWORD_DEFAULT);
  query('UPDATE supervisors SET password_hash=? WHERE id=?',[$newHash,$u['id']]);
  $_SESSION['pwd_fp']=hash('sha256',$newHash);
  session_regenerate_id(true);audit('password_change','supervisor',(string)$u['id']);
  notify('success','Password changed. Other existing sessions will be signed out.');redirect('profile');
 case 'profile_photo':
  try { profile_photo_upload($u);audit('avatar_update','supervisor',(string)$u['id']);notify('success','Profile photo updated successfully.'); }
  catch(Throwable $err) {error_log('Profile image update: '.$err->getMessage());notify('error','Profile photo could not be saved: '.$err->getMessage());}
  redirect('profile');
 case 'remove_profile_photo':
  $file=profile_photo_file((int)$u['id']);
  if(is_file($file) && !@unlink($file)){notify('error','Could not remove the profile photo. Check hosting file permissions.');redirect('profile');}
  audit('avatar_removed','supervisor',(string)$u['id']);notify('success','Profile photo removed.');redirect('profile');
 default: http_response_code(404);exit('Unknown action');
 }
}
