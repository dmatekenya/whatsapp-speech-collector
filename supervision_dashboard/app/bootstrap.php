<?php
declare(strict_types=1);
const APP_VERSION = '0.0.0.2';
const APP_TARGET_HOURS = 500;
const APP_TIMEZONE = 'Africa/Blantyre';
ini_set('display_errors','0');
ini_set('log_errors','1');
date_default_timezone_set('UTC');
if (PHP_SAPI !== 'cli') {
    header('X-Content-Type-Options: nosniff');
    header('Referrer-Policy: same-origin');
    header('X-Frame-Options: DENY');
    header("Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self'; connect-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'");
    if (session_status() === PHP_SESSION_NONE) {
        ini_set('session.use_strict_mode','1');
        session_set_cookie_params(['lifetime'=>0,'path'=>'/','secure'=>(!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS']!=='off'), 'httponly'=>true,'samesite'=>'Lax']);
        session_start();
    }
}
function cfg(): array {
 static $conf=null;
 if($conf===null){
  $base=dirname(__DIR__).'/private';$config=$base.'/config.php';
  if(!is_file($config))throw new RuntimeException('Not installed');
  $conf=require $config;
  if(!is_array($conf))throw new RuntimeException('Invalid private configuration');
  // Separate, protected QC credential. Existing database/API settings stay untouched.
  $secretFile=$base.'/qc_review_token.php';
  if(is_file($secretFile) && !is_link($secretFile)){
   $token=require $secretFile;
   if(is_string($token)&&preg_match('/^[a-f0-9]{96}$/D',$token))$conf['review_api_token']=$token;
  }
 }
 return $conf;
}
function db(): PDO { static $pdo=null; if($pdo===null) { $c=cfg(); $pdo=new PDO('mysql:host='.$c['db_host'].';port='.((int)($c['db_port']??3306)).';dbname='.$c['db_name'].';charset=utf8mb4',$c['db_user'],$c['db_pass'],[PDO::ATTR_ERRMODE=>PDO::ERRMODE_EXCEPTION,PDO::ATTR_DEFAULT_FETCH_MODE=>PDO::FETCH_ASSOC,PDO::ATTR_EMULATE_PREPARES=>false]); } return $pdo; }
function query(string $sql,array $params=[]): PDOStatement { $s=db()->prepare($sql); $s->execute($params); return $s; }
function setting(string $name, ?string $default=null): ?string { $s=query('SELECT setting_value FROM app_settings WHERE setting_key=?',[$name])->fetchColumn(); return $s===false?$default:(string)$s; }
function set_setting(string $name,string $value): void { query('INSERT INTO app_settings(setting_key,setting_value) VALUES (?,?) ON DUPLICATE KEY UPDATE setting_value=VALUES(setting_value)',[$name,$value]); }
function h(mixed $s): string {return htmlspecialchars((string)($s??''),ENT_QUOTES|ENT_SUBSTITUTE,'UTF-8');}
function linkto(string $page,array $params=[]): string {return 'index.php?'.http_build_query(array_merge(['page'=>$page],$params), '', '&', PHP_QUERY_RFC3986);}
function csrf(): string {if(empty($_SESSION['csrf'])) $_SESSION['csrf']=bin2hex(random_bytes(32)); return $_SESSION['csrf'];}
function csrf_input(): string {return '<input type="hidden" name="csrf" value="'.h(csrf()).'">';}
function check_csrf(): void {if(!isset($_POST['csrf'])||!hash_equals(csrf(),(string)$_POST['csrf'])){http_response_code(419);exit('Security check failed. Refresh and retry.');}}
function redirect(string $page,array $params=[]): never {header('Location: '.linkto($page,$params),true,303); exit;}
function viewer(): ?array {
 if(empty($_SESSION['uid'])) return null;
 static $v=null;
 if($v===null){
  $record=query('SELECT id,display_name,email,role,active,password_hash,last_login_at FROM supervisors WHERE id=?',[(int)$_SESSION['uid']])->fetch()?:null;
  $good=$record && (int)$record['active']===1 && isset($_SESSION['pwd_fp']) && hash_equals((string)$_SESSION['pwd_fp'],hash('sha256',(string)$record['password_hash']));
  if(!$good){ unset($_SESSION['uid'],$_SESSION['pwd_fp']);return null; }
  unset($record['password_hash']);$v=$record;
 }
 return $v;
}
function require_user(): array { $u=viewer();if(!$u) redirect('login');return $u; }
function require_admin(): array { $u=require_user(); if($u['role']!=='admin'){http_response_code(403);exit('Access denied.');}return $u; }
function audit(string $action,?string $type=null,?string $id=null,string $details=''): void { $u=viewer(); query('INSERT INTO audit_log(actor_id,action,subject_type,subject_id,details) VALUES (?,?,?,?,?)',[$u['id']??null,$action,$type,$id,mbcut($details,250)]); }
function mbcut(string $str,int $max): string {return function_exists('mb_substr')?mb_substr($str,0,$max):substr($str,0,$max);}
function notify(string $type,string $message): void {$_SESSION['flash'][]=[$type,$message];}
function flash_html(): string { $list=$_SESSION['flash']??[];unset($_SESSION['flash']);$out='';foreach($list as [$type,$msg])$out.='<div class="notice '.h($type).'">'.h($msg).'</div>';return $out; }
function date_ml(?string $utc, bool $withTime=true): string {if(!$utc)return '—';try{$t=new DateTimeImmutable($utc,new DateTimeZone('UTC'));return $t->setTimezone(new DateTimeZone(APP_TIMEZONE))->format($withTime?'d M Y, H:i':'d M Y');}catch(Throwable){return '—';}}
function ml_day_bounds(string $date): array {if(!preg_match('/^\d{4}-\d{2}-\d{2}$/',$date)||!($dt=DateTimeImmutable::createFromFormat('!Y-m-d',$date,new DateTimeZone(APP_TIMEZONE)))||$dt->format('Y-m-d')!==$date) throw new InvalidArgumentException('Invalid date'); return [$dt->setTimezone(new DateTimeZone('UTC'))->format('Y-m-d H:i:s'),$dt->modify('+1 day')->setTimezone(new DateTimeZone('UTC'))->format('Y-m-d H:i:s')];}
function ml_today(): string { return (new DateTimeImmutable('now',new DateTimeZone(APP_TIMEZONE)))->format('Y-m-d'); }
function allowed_record(int $id,array $user): bool {if($user['role']==='admin')return (bool)query('SELECT 1 FROM recordings WHERE id=?',[$id])->fetchColumn(); return (bool)query('SELECT 1 FROM recordings r JOIN supervisor_assignments a ON a.participant_id=r.participant_id AND a.supervisor_id=? WHERE r.id=?',[(int)$user['id'],$id])->fetchColumn();}
function scope_sql(array $u,string $alias='r'): array {return $u['role']==='admin'?['',[]]:[' AND EXISTS (SELECT 1 FROM supervisor_assignments sa WHERE sa.supervisor_id=? AND sa.participant_id='.$alias.'.participant_id)',[(int)$u['id']]];}
function count_value(string $sql,array $p=[]): int {return (int)query($sql,$p)->fetchColumn();}
function status_label(?string $status): string {return match($status){'needs_review'=>'Needs review','pending'=>'Pending','accepted'=>'Accepted','rejected'=>'Rejected',default=>ucfirst((string)$status)};}
function duration(int|float|null $ms): string {if($ms===null)return '—';$sec=(float)$ms/1000;return number_format($sec,1).' s';}
function sync_ready(): bool {$c=cfg(); return !empty($c['api_url'])&&!empty($c['api_token']);}
function sync_timestamp(): ?string{return setting('last_successful_sync');}
function sync_fresh(): bool { $s=sync_timestamp();return $s!==null && strtotime($s)>=time()-900; }
function require_setup(): void {if(!is_file(dirname(__DIR__).'/private/config.php')){header('Location: install.php',true,302);exit;}}
