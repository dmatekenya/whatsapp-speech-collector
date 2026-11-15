<?php
declare(strict_types=1);
require __DIR__.'/app/bootstrap.php';
require_setup();
require __DIR__.'/app/integration.php';
require __DIR__.'/app/reviews.php';
require __DIR__.'/app/profile.php';
require __DIR__.'/app/view.php';
require __DIR__.'/app/pages.php';
require __DIR__.'/app/admin_pages.php';
require __DIR__.'/app/actions.php';
$page=(string)($_GET['page']??'overview');
if($page==='health'){header('Content-Type: application/json');echo json_encode(['ok'=>true,'application'=>'Speech Quality','version'=>APP_VERSION]);exit;}
if($page==='login'){
 if(viewer())redirect('overview');
 if($_SERVER['REQUEST_METHOD']==='POST'){
  check_csrf();$email=strtolower(trim((string)($_POST['email']??'')));$pass=(string)($_POST['password']??'');
  $digest=hash('sha256',(string)($_SERVER['REMOTE_ADDR']??'unknown'));
  $attempt=query('SELECT attempts,locked_until,window_started FROM login_attempts WHERE ip_digest=?',[$digest])->fetch();
  $locked=$attempt && $attempt['locked_until'] && strtotime($attempt['locked_until'])>time();
  $user=$locked?null:query('SELECT * FROM supervisors WHERE email=? AND active=1',[$email])->fetch();
  if($user&&password_verify($pass,$user['password_hash'])){
    query('DELETE FROM login_attempts WHERE ip_digest=?',[$digest]);session_regenerate_id(true);$_SESSION['uid']=(int)$user['id'];$_SESSION['pwd_fp']=hash('sha256',(string)$user['password_hash']);$_SESSION['csrf']=bin2hex(random_bytes(32));query('UPDATE supervisors SET last_login_at=UTC_TIMESTAMP() WHERE id=?',[$user['id']]);audit('login','supervisor',(string)$user['id']);redirect('overview');
  }
  $window=$attempt && strtotime($attempt['window_started'])>time()-900 ? (int)$attempt['attempts']+1 : 1;
  $until=$window>=6?gmdate('Y-m-d H:i:s',time()+900):null;
  query('INSERT INTO login_attempts(ip_digest,attempts,window_started,locked_until) VALUES (?,?,UTC_TIMESTAMP(),?) ON DUPLICATE KEY UPDATE attempts=VALUES(attempts),window_started=IF(?=1,UTC_TIMESTAMP(),window_started),locked_until=VALUES(locked_until)',[$digest,$window,$until,$window]);
  notify('error','Login unsuccessful. Check credentials or try again later.');
 }
 auth_page('Sign in','<form method="post" class="stack">'.csrf_input().'<label>Email address<input type="email" name="email" autocomplete="username" required></label><label>Password<input type="password" name="password" autocomplete="current-password" required></label><button class="btn primary" type="submit">Sign in</button></form>');exit;
}
if($page==='logout'){if($_SERVER['REQUEST_METHOD']!=='POST'){http_response_code(405);exit('POST required');}check_csrf();$_SESSION=[];session_regenerate_id(true);redirect('login');}
try{
 $u=require_user();
 if($page==='live_refresh'){
  if($_SERVER['REQUEST_METHOD']!=='POST'){http_response_code(405);exit('POST required');}
  check_csrf();
  // Avoid holding the session file lock during the remote metadata request.
  if(session_status()===PHP_SESSION_ACTIVE)session_write_close();
  header('Content-Type: application/json; charset=utf-8');header('Cache-Control: no-store');
  echo json_encode(live_refresh($u),JSON_THROW_ON_ERROR);exit;
 }
 if($page==='audio_status'){audio_status_response(max(0,(int)($_GET['id']??0)),$u);}
 if($page==='api_status'){
  [$scope,$scopeArgs]=scope_sql($u);
  $n=count_value('SELECT COUNT(*) FROM recordings r WHERE r.id > COALESCE((SELECT last_seen_recording_id FROM notification_state WHERE user_id=?),0)'.$scope,array_merge([(int)$u['id']],$scopeArgs));
  header('Content-Type: application/json; charset=utf-8');header('Cache-Control: no-store');
  echo json_encode(['unread'=>$n,'last_sync'=>sync_timestamp(),'connector'=>sync_ready()]);exit;
 }
 if($page==='avatar'){profile_photo_serve($u,max(0,(int)($_GET['uid']??0)));}
 if($page==='audio'){audio_response(max(0,(int)($_GET['id']??0)),$u);}
 if($page==='export'){export_csv($u);}
 if($page==='audit_export'){audit_export_reviews();}
 if($_SERVER['REQUEST_METHOD']==='POST'){post_action($page,$u);}
 $title=ucwords(str_replace('_',' ',$page));$body='';
 switch($page){
  case 'overview':$title='Overview';$body=overview_page($u);break;
  case 'production':$title='Production snapshot';$body=production_page($u);break;
  case 'recordings':$title='Recording history';$body=record_listing($u,'recordings');break;
  case 'voices':$title='Production voices';$body=record_listing($u,'voices');break;
  case 'review':$title='Production review workspace';$body=record_listing($u,'review');break;
  case 'queue':$title='Review queue';$body=record_listing($u,'queue');break;
  case 'notifications':$title='New recordings';$body=record_listing($u,'notifications');break;
  case 'contributors':$title='Production contributors';$body=contributors_page($u);break;
  case 'contributor':$title='Contributor details';$body=contributor_detail_page($u,(string)($_GET['id']??''));break;
  case 'voice':$title='Production recording';$body=voice_page($u,max(0,(int)($_GET['id']??0)));break;
  case 'followups':$title='Follow-ups';$body=followups_page($u);break;
  case 'reports':$title='Daily reports';$body=report_page($u);break;
  case 'supervisors':$title='Supervisors';$body=supervisors_page($u);break;
  case 'assignments':$title='Supervisor assignments';$body=assignments_page($u);break;
  case 'audit':$title='Action log';$body=audit_page($u);break;
  case 'feedback':$title='Feedback delivery';$body=feedback_page($u);break;
  case 'integration':$title='Integration status';$body=integration_page($u);break;
  case 'settings':$title='System settings';$body=settings_page($u);break;
  case 'profile':$title='Profile & security';$body=profile_page($u);break;
  default:http_response_code(404);$title='Page not found';$body=page_top('Page not found','The page you requested is not available.');break;
 }
 app_layout($page,$title,$body,$u);
}catch(Throwable $e){error_log('Speech Quality request failed: '.$e->getMessage());http_response_code(500);?><!doctype html><html><head><meta charset="utf-8"><title>Dashboard unavailable</title><link rel="stylesheet" href="assets/app.css"></head><body class="auth-page"><div class="auth-card"><h1>Dashboard temporarily unavailable</h1><p>We could not complete this request. Check the PHP error log and database connection.</p><a class="btn outline" href="index.php?page=overview">Try again</a></div></body></html><?php }
