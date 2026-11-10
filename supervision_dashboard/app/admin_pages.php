<?php
declare(strict_types=1);
function supervisors_page(array $u): string {
 require_admin();
 $users=query('SELECT id,display_name,email,role,active,last_login_at FROM supervisors ORDER BY role,id')->fetchAll();
 $counts=query('SELECT supervisor_id,COUNT(*) AS n FROM supervisor_assignments GROUP BY supervisor_id')->fetchAll();$byId=[];foreach($counts as $c)$byId[(int)$c['supervisor_id']]=(int)$c['n'];
 $unassigned=count_value('SELECT COUNT(*) FROM contributors c WHERE NOT EXISTS(SELECT 1 FROM supervisor_assignments a WHERE a.participant_id=c.participant_id)');
 $html=page_top('Supervisor management','Create reviewer accounts and allocate contributors. Every supervisor sees only explicitly assigned contributors.','ADMINISTRATION · TEAM ACCESS');
 $html.='<div class="metrics three">'.box_metric('Supervisors',(string)count(array_filter($users,fn($x)=>$x['role']==='supervisor')),'Registered reviewer accounts').box_metric('Unassigned contributors',(string)$unassigned,'Contributors needing allocation').box_metric('Administrator access','All contributors','Complete oversight').'</div>';
 $html.=panel_start('Create supervisor','Each supervisor has their own sign-in and no recording access until assigned.');
 $html.='<form method="post" action="'.h(linkto('create_supervisor')).'" class="formgrid triple">'.csrf_input().'<label>Full display name<input name="name" required maxlength="120" autocomplete="off"></label><label>Email address<input name="email" type="email" required autocomplete="off"></label><label>Temporary password (14+ characters)<input name="password" type="password" minlength="14" autocomplete="new-password" required></label><button class="btn primary" type="submit">Create supervisor</button></form></section>';
 $html.=panel_start('Accounts and permissions','Select Manage assignments to assign or reassign contributor IDs in bulk.');
 $html.='<div class="table-scroll"><table><thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Assigned contributors</th><th>Status</th><th>Last sign-in</th><th>Action</th></tr></thead><tbody>';
 foreach($users as $x){$id=(int)$x['id'];$html.='<tr><td><strong>'.h($x['display_name']).'</strong></td><td>'.h($x['email']).'</td><td>'.h(ucfirst($x['role'])).'</td><td>'.($x['role']==='admin'?'Full access':(string)($byId[$id]??0)).'</td><td><span class="pill '.($x['active']?'accepted':'rejected').'">'.($x['active']?'Active':'Inactive').'</span></td><td>'.h(date_ml($x['last_login_at'])).'</td><td>'.($x['role']==='admin'?'Full oversight':'<a class="btn small outline" href="'.h(linkto('assignments',['user'=>$id])).'">Manage assignments</a>').'</td></tr>';}
 return $html.'</tbody></table></div></section>';
}
function assignments_page(array $u): string {
 require_admin();$id=(int)($_GET['user']??0);$person=query("SELECT id,display_name,email,active FROM supervisors WHERE id=? AND role='supervisor'",[$id])->fetch();
 if(!$person)return page_top('Supervisor not found','Select a supervisor on the team management page.').'<a class="btn outline" href="'.h(linkto('supervisors')).'">Back to supervisors</a>';
 $assigned=query('SELECT participant_id FROM supervisor_assignments WHERE supervisor_id=? ORDER BY participant_id',[$id])->fetchAll(PDO::FETCH_COLUMN);
 $all=query('SELECT c.participant_id,c.contributor_type,c.status,COUNT(r.id) AS voice_count, GROUP_CONCAT(DISTINCT CONCAT(s.display_name, " (#",s.id,")") SEPARATOR ", ") AS assigned_to FROM contributors c LEFT JOIN recordings r ON r.participant_id=c.participant_id LEFT JOIN supervisor_assignments sa ON sa.participant_id=c.participant_id LEFT JOIN supervisors s ON s.id=sa.supervisor_id GROUP BY c.participant_id,c.contributor_type,c.status ORDER BY c.participant_id')->fetchAll();
 $html=page_top('Assign contributors · '.$person['display_name'],'Select contributor IDs from the synchronized production catalog. Reassignment transfers visibility to this supervisor.','ADMINISTRATION · ACCESS CONTROL');
 $html.='<div class="assignment-summary"><a class="btn outline" href="'.h(linkto('supervisors')).'">← All supervisors</a><span class="pill '.($person['active']?'accepted':'rejected').'">'.($person['active']?'Active account':'Account disabled').'</span><strong>'.count($assigned).' assigned</strong><span class="muted">'.h($person['email']).'</span></div>';
 $html.=panel_start('Assign multiple contributors','Tick the contributors this supervisor should manage, then select Assign selected. Existing allocations for those IDs are transferred safely.');
 $html.='<form method="post" action="'.h(linkto('assignment_batch')).'" id="bulk-assignment-form">'.csrf_input().'<input type="hidden" name="user" value="'.$id.'">';
 $html.='<div class="assignment-toolbar"><label>Filter contributors<input type="search" data-assignment-search placeholder="Search contributor ID"></label><label class="assign-select-all"><input type="checkbox" data-assignment-select-all> Select visible</label><span class="muted" data-assignment-count>0 selected</span><button class="btn primary" type="submit" '.(!$person['active']?'disabled':'').'>Assign selected</button></div>';
 $html.='<div class="assignment-grid">';
 foreach($all as $row){$pid=$row['participant_id'];$sel=in_array($pid,$assigned,true);$html.='<label class="assignment-card" data-assignment-card data-pid="'.h(strtolower($pid)).'"><input type="checkbox" name="contributors[]" value="'.h($pid).'" data-assignment-checkbox><span><strong>'.h($pid).'</strong><small>'.h(ucfirst($row['contributor_type'])).' · '.(int)$row['voice_count'].' voices</small><small>'.($sel?'Currently assigned to this supervisor':($row['assigned_to']?'Assigned to '.h($row['assigned_to']):'Unassigned')).'</small></span></label>';}
 if(!$all)$html.='<p class="no-data">No synchronized contributors are available yet.</p>';
 $html.='</div></form></section>';
 $html.=panel_start('Currently assigned · '.count($assigned),'Use Remove to revoke an individual contributor immediately.');
 if(!$assigned)$html.='<div class="no-data">No contributors assigned. This supervisor cannot access production audio or review records.</div>';
 foreach($assigned as $pid)$html.='<form class="assignment-row" method="post" action="'.h(linkto('assignment_remove')).'">'.csrf_input().'<input type="hidden" name="user" value="'.$id.'"><input type="hidden" name="participant_id" value="'.h($pid).'"><strong>'.h($pid).'</strong><a href="'.h(linkto('voices',['search'=>$pid])).'">View voices</a><button class="btn small outline" type="submit">Remove access</button></form>';
 $html.='</section><section class="panel"><h2>Supervisor account</h2><form method="post" action="'.h(linkto('toggle_supervisor')).'">'.csrf_input().'<input type="hidden" name="user" value="'.$id.'"><button class="btn outline" type="submit">'.($person['active']?'Deactivate supervisor':'Reactivate supervisor').'</button></form></section>';
 return $html;
}
/**
 * Masakhane Action Log: read-only reporting over existing application tables.
 * No migrations, status updates or DigitalOcean calls are performed here.
 */
function audit_get(string $key, string $default=''): string {
 $value=$_GET[$key]??$default;
 return is_string($value)?trim($value):$default;
}
function audit_db_columns(string $table): array {
 static $cache=[];
 $allowed=['review_drafts','recordings','supervisors','sync_runs','audit_log'];
 if(!in_array($table,$allowed,true))return [];
 if(array_key_exists($table,$cache))return $cache[$table];
 try {
  $rows=query('SHOW COLUMNS FROM `'.$table.'`')->fetchAll();
  $cache[$table]=array_fill_keys(array_column($rows,'Field'),true);
 }catch(Throwable $e){
  error_log('Masakhane Action Log schema check ('.$table.'): '.$e->getMessage());
  $cache[$table]=[];
 }
 return $cache[$table];
}
function audit_schema_ready(): bool {
 $required=[
  'review_drafts'=>['id','recording_id','reviewer_id','decision','created_at'],
  'recordings'=>['id','participant_id'],
  'supervisors'=>['id','display_name']
 ];
 foreach($required as $table=>$fields){
  $columns=audit_db_columns($table);
  foreach($fields as $field)if(!isset($columns[$field]))return false;
 }
 return true;
}
function audit_col(string $table,string $column,string $expression): string {
 return isset(audit_db_columns($table)[$column])?$expression:'NULL';
}
function audit_prompt_name(?string $prompt): string {
 return match($prompt){
  'sentence'=>'Reading sentences', 'image'=>'Pictures',
  'free_speech'=>'Free speech', 'conversation_starter'=>'Conversation starter',
  'unclassified',null,''=>'Unclassified', default=>'Other / '.(string)$prompt
 };
}
function audit_date_bounds(string $date): ?array {
 if(!preg_match('/^\d{4}-\d{2}-\d{2}$/D',$date))return null;
 try {return ml_day_bounds($date);} catch(Throwable){return null;}
}
/** Build SQL filters only from allow-listed request values. */
function audit_review_filters(): array {
 $participant=audit_get('contributor');
 if($participant!==''&&!preg_match('/^[A-Za-z0-9_-]{1,80}$/D',$participant))$participant='';
 $supervisor=audit_get('supervisor');
 if($supervisor!==''&&(!ctype_digit($supervisor)||strlen($supervisor)>10))$supervisor='';
 $prompt=audit_get('prompt');
 if(!in_array($prompt,['','sentence','image','free_speech','conversation_starter','unclassified'],true))$prompt='';
 $decision=audit_get('decision');
 // Historical escalations are visible for auditing; no escalation action is added.
 if(!in_array($decision,['','approve','reject','flag','escalate'],true))$decision='';
 $state=audit_get('state');
 if(!in_array($state,['','draft','pending','confirmed','failed','conflict'],true))$state='';
 $from=audit_get('from');$to=audit_get('to');
 $fromBounds=$from!==''?audit_date_bounds($from):null;
 $toBounds=$to!==''?audit_date_bounds($to):null;
 if($from!==''&&$fromBounds===null)$from='';
 if($to!==''&&$toBounds===null)$to='';
 $where=' WHERE 1=1';$params=[];
 if($participant!==''){$where.=' AND r.participant_id=?';$params[]=$participant;}
 if($supervisor!==''){$where.=' AND d.reviewer_id=?';$params[]=(int)$supervisor;}
 if($prompt==='unclassified'){
  if(isset(audit_db_columns('recordings')['prompt_type']))
   $where.=" AND (r.prompt_type IS NULL OR r.prompt_type='' OR r.prompt_type NOT IN ('sentence','image','free_speech','conversation_starter'))";
  else $where.=' AND 1=1';
 }elseif($prompt!==''){
  if(isset(audit_db_columns('recordings')['prompt_type'])){$where.=' AND r.prompt_type=?';$params[]=$prompt;}
  else $where.=' AND 1=0';
 }
 if($decision!==''){$where.=' AND d.decision=?';$params[]=$decision;}
 if($state!==''){
  if(isset(audit_db_columns('review_drafts')['production_state'])){$where.=' AND d.production_state=?';$params[]=$state;}
  else $where.=' AND 1=0';
 }
 if($fromBounds!==null){$where.=' AND d.created_at>=?';$params[]=$fromBounds[0];}
 if($toBounds!==null){$where.=' AND d.created_at<?';$params[]=$toBounds[1];}
 return [$where,$params,[
  'contributor'=>$participant,'supervisor'=>$supervisor,'prompt'=>$prompt,
  'decision'=>$decision,'state'=>$state,'from'=>$from,'to'=>$to
 ]];
}
function audit_reviews_from(): string {
 return ' FROM review_drafts d LEFT JOIN supervisors s ON s.id=d.reviewer_id LEFT JOIN recordings r ON r.id=d.recording_id ';
}
function audit_production_expr(): string {return audit_col('review_drafts','production_state','d.production_state');}
function audit_confirmed_at_expr(): string {return audit_col('review_drafts','confirmed_at','d.confirmed_at');}
function audit_prompt_expr(): string {return audit_col('recordings','prompt_type','r.prompt_type');}
function audit_notes_expr(): string {return audit_col('review_drafts','notes','d.notes');}
function audit_group_counters(): string {
 $state=audit_production_expr();
 return "COUNT(*) AS total_reviews, COUNT(DISTINCT d.recording_id) AS distinct_recordings, ".
  "COALESCE(SUM(CASE WHEN d.decision='approve' AND $state='confirmed' THEN 1 ELSE 0 END),0) AS approved, ".
  "COALESCE(SUM(CASE WHEN d.decision='reject' AND $state='confirmed' THEN 1 ELSE 0 END),0) AS rejected, ".
  "COALESCE(SUM(CASE WHEN d.decision='flag' THEN 1 ELSE 0 END),0) AS notes, ".
  "COALESCE(SUM(CASE WHEN $state IN ('pending','failed','conflict') THEN 1 ELSE 0 END),0) AS unconfirmed";
}
function audit_review_breakdown(string $groupSql,string $where,array $params,int $limit): array {
 $limit=max(1,min($limit,500));
 $sql='SELECT '.$groupSql.' AS group_name, '.audit_group_counters().audit_reviews_from().$where.
      ' GROUP BY '.$groupSql.' ORDER BY total_reviews DESC, group_name ASC LIMIT '.$limit;
 return query($sql,$params)->fetchAll();
}
function audit_review_breakdown_table(string $title,array $rows): string {
 $html='<section class="panel audit-breakdown"><h2>'.h($title).'</h2><div class="table-scroll"><table><thead><tr><th>Category</th><th>Reviews</th><th>Recordings</th><th>Approved</th><th>Rejected</th><th>Notes</th><th>Unconfirmed</th></tr></thead><tbody>';
 foreach($rows as $r){
  $category=$title==='Totals by prompt type'?audit_prompt_name($r['group_name']??null):($r['group_name']??'Unknown');
  $html.='<tr><td>'.h($category).'</td><td>'.number_format((int)$r['total_reviews']).'</td><td>'.number_format((int)$r['distinct_recordings']).'</td><td>'.number_format((int)$r['approved']).'</td><td>'.number_format((int)$r['rejected']).'</td><td>'.number_format((int)$r['notes']).'</td><td>'.number_format((int)$r['unconfirmed']).'</td></tr>';
 }
 if(!$rows)$html.='<tr><td colspan="7" class="no-data">No matching reviews.</td></tr>';
 return $html.'</tbody></table></div></section>';
}
function audit_record_select(): string {
 return 'SELECT d.id,d.created_at,d.recording_id,d.reviewer_id,d.decision,'.
  audit_production_expr().' AS production_state,'.
  audit_notes_expr().' AS notes,'.
  audit_confirmed_at_expr().' AS confirmed_at,'.
  "COALESCE(s.display_name,CONCAT('Supervisor #',d.reviewer_id)) AS display_name,".
  'r.participant_id,'.audit_prompt_expr().' AS prompt_type';
}
function audit_export_reviews(): never {
 require_admin();
 if(!audit_schema_ready()) {http_response_code(503);exit('Review history schema is unavailable. No data was exported.');}
 [$where,$params]=audit_review_filters();
 try {
  $rows=query(audit_record_select().audit_reviews_from().$where.' ORDER BY d.id DESC LIMIT 10000',$params)->fetchAll();
 }catch(Throwable $e){error_log('Masakhane Action Log CSV: '.$e->getMessage());http_response_code(503);exit('Action Log export unavailable. Check application error log.');}
 header('Content-Type: text/csv; charset=utf-8');
 header('Content-Disposition: attachment; filename="masakhane-qc-action-log-'.gmdate('Ymd-His').'.csv"');
 header('Cache-Control: private, no-store, max-age=0');
 header('X-Content-Type-Options: nosniff');
 $stream=fopen('php://output','wb');
 fputcsv($stream,['When (Malawi)','Supervisor','Contributor ID','Prompt type','Recording ID','Decision','Production state','Reason / notes','Confirmed (Malawi)']);
 foreach($rows as $r){
  $out=[date_ml($r['created_at']),(string)$r['display_name'],(string)($r['participant_id']??''),audit_prompt_name($r['prompt_type']??null),(string)$r['recording_id'],(string)$r['decision'],(string)($r['production_state']??'not available'),(string)($r['notes']??''),date_ml($r['confirmed_at']??null)];
  // Prevent spreadsheet formula execution, while preserving readable reviewer notes.
  foreach($out as &$cell){$cell=(string)$cell;if(preg_match('/^[\x00-\x20]*[=+@-]/u',$cell))$cell="'".$cell;}
  unset($cell);fputcsv($stream,$out);
 }
 fclose($stream);exit;
}
function audit_page(array $u): string {
 require_admin();
 $tab=audit_get('tab','reviews');
 if(!in_array($tab,['reviews','accounts','sync'],true))$tab='reviews';
 $html=page_top('Action log','Filter supervisor decisions by contributor, reviewer, prompt type and date. Only confirmed production decisions count as approved or rejected.');
 $html.='<div class="tab-row"><a class="btn '.($tab==='reviews'?'primary':'outline').'" href="'.h(linkto('audit',['tab'=>'reviews'])).'">Supervisor review decisions</a><a class="btn '.($tab==='accounts'?'primary':'outline').'" href="'.h(linkto('audit',['tab'=>'accounts'])).'">Account & access</a><a class="btn '.($tab==='sync'?'primary':'outline').'" href="'.h(linkto('audit',['tab'=>'sync'])).'">Sync history</a></div>';
 if($tab==='sync'){
  $html.=panel_start('Production synchronization history','Recent production metadata synchronization operations.');
  $html.='<div class="table-scroll"><table><thead><tr><th>Started</th><th>Status</th><th>Summary</th><th>Finished</th></tr></thead><tbody>';
  try {
   $rows=query('SELECT started_at,status,description,finished_at FROM sync_runs ORDER BY id DESC LIMIT 200')->fetchAll();
   foreach($rows as $r)$html.='<tr><td>'.h(date_ml($r['started_at'])).'</td><td>'.h($r['status']).'</td><td>'.h($r['description']).'</td><td>'.h(date_ml($r['finished_at'])).'</td></tr>';
   if(!$rows)$html.='<tr><td colspan="4" class="no-data">No synchronization entries yet.</td></tr>';
  } catch(Throwable $e){error_log('Masakhane Action Log sync history: '.$e->getMessage());$html.='<tr><td colspan="4" class="no-data">Synchronization history is temporarily unavailable. Check the PHP error log.</td></tr>';}
  return $html.'</tbody></table></div></section>';
 }
 if($tab==='accounts'){
  $html.=panel_start('Account and access activity','Last 200 account, recording access and administration events.');
  $html.='<div class="table-scroll"><table><thead><tr><th>When</th><th>Actor</th><th>Action</th><th>Subject</th></tr></thead><tbody>';
  try{
   $rows=query('SELECT l.created_at,l.action,l.subject_type,l.subject_id,s.display_name FROM audit_log l LEFT JOIN supervisors s ON s.id=l.actor_id ORDER BY l.id DESC LIMIT 200')->fetchAll();
   foreach($rows as $r)$html.='<tr><td>'.h(date_ml($r['created_at'])).'</td><td>'.h($r['display_name']??'System').'</td><td>'.h($r['action']).'</td><td>'.h(trim((string)($r['subject_type']??'').' #'.(string)($r['subject_id']??''))).'</td></tr>';
   if(!$rows)$html.='<tr><td colspan="4" class="no-data">No account activity yet.</td></tr>';
  }catch(Throwable $e){error_log('Masakhane Action Log access: '.$e->getMessage());$html.='<tr><td colspan="4" class="no-data">Account history is temporarily unavailable. Check the PHP error log.</td></tr>';}
  return $html.'</tbody></table></div></section>';
 }
 if(!audit_schema_ready()){
  $html.=panel_start('Review history not available','The existing review tables or required columns are not available to this application. No changes have been made to any recording or QC decision.');
  return $html.'<p class="notice warning">Use the supplied terminal diagnostic to check which database fields require attention. Review editing is not performed by this page.</p></section>';
 }
 [$where,$params,$filters]=audit_review_filters();
 try{$reviewers=query('SELECT id,display_name FROM supervisors ORDER BY display_name,id')->fetchAll();}
 catch(Throwable $e){error_log('Masakhane Action Log supervisors: '.$e->getMessage());$reviewers=[];}
 $options=[''=>'All supervisors'];foreach($reviewers as $row)$options[(string)$row['id']]=$row['display_name'];
 $html.='<section class="panel audit-filter-panel"><h2>Filter review decisions</h2><form method="get" class="audit-filter-grid"><input type="hidden" name="page" value="audit"><input type="hidden" name="tab" value="reviews">';
 $html.='<label>Contributor ID<input name="contributor" value="'.h($filters['contributor']).'" maxlength="80" placeholder="P0014"></label>';
 $html.='<label>Supervisor<select name="supervisor">'.select_options($options,$filters['supervisor']).'</select></label>';
 $html.='<label>Prompt type<select name="prompt">'.select_options([''=>'All prompt types','sentence'=>'Reading sentences','image'=>'Pictures','free_speech'=>'Free speech','conversation_starter'=>'Conversation starter','unclassified'=>'Unclassified'],$filters['prompt']).'</select></label>';
 $html.='<label>Decision<select name="decision">'.select_options([''=>'All decisions','approve'=>'Approve','reject'=>'Reject','flag'=>'Note / flag','escalate'=>'Historical escalation'],$filters['decision']).'</select></label>';
 $html.='<label>Production status<select name="state">'.select_options([''=>'All statuses','confirmed'=>'Confirmed','draft'=>'Draft / note','pending'=>'Pending delivery','failed'=>'Delivery failed','conflict'=>'Conflict'],$filters['state']).'</select></label>';
 $html.='<label>From date (Malawi)<input type="date" name="from" value="'.h($filters['from']).'"></label><label>To date (Malawi)<input type="date" name="to" value="'.h($filters['to']).'"></label>';
 $filtered=array_filter($filters,static fn($value)=>$value!=='');
 $html.='<div class="form-buttons"><button class="btn primary" type="submit">Apply filters</button><a class="btn outline" href="'.h(linkto('audit',['tab'=>'reviews'])).'">Clear</a><a class="btn outline" href="'.h(linkto('audit_export',$filtered)).'">Export CSV</a></div></form></section>';
 try {
  $summary=query('SELECT '.audit_group_counters().audit_reviews_from().$where,$params)->fetch();
  $page=max(1,min(100000,(int)($_GET['p']??1)));$perPage=25;$offset=($page-1)*$perPage;
  $rows=query(audit_record_select().audit_reviews_from().$where.' ORDER BY d.id DESC LIMIT '.$perPage.' OFFSET '.$offset,$params)->fetchAll();
  $state=audit_production_expr();
  $bySupervisor=audit_review_breakdown("CONCAT(COALESCE(s.display_name,'Unknown'),' (#',d.reviewer_id,')')",$where,$params,100);
  $byPrompt=audit_review_breakdown('COALESCE(NULLIF('.audit_prompt_expr().',\'\'),\'unclassified\')',$where,$params,20);
  $byContributor=audit_review_breakdown("COALESCE(NULLIF(r.participant_id,''),'unknown')",$where,$params,250);
 }catch(Throwable $e){
  error_log('Masakhane Action Log review query: '.$e->getMessage());
  $html.=panel_start('Review history unavailable','The reporting query failed. Existing production recordings and QC decisions have not been modified.');
  return $html.'<p class="notice warning">Run the supplied terminal diagnostic and check the Hostinger PHP error log for the cause.</p></section>';
 }
 $html.='<div class="metrics four">'.box_metric('Matching reviews',number_format((int)($summary['total_reviews']??0)),'Filtered submissions').box_metric('Confirmed approved',number_format((int)($summary['approved']??0)),'Official production status').box_metric('Confirmed rejected',number_format((int)($summary['rejected']??0)),'Official production status').box_metric('Not yet confirmed',number_format((int)($summary['unconfirmed']??0)),'Pending / failed / conflict').'</div>';
 $total=(int)($summary['total_reviews']??0);
 $html.=panel_start('Supervisor review history',number_format($total).' matching review submissions · '.$perPage.' per page · Malawi local time');
 $html.='<div class="table-scroll"><table><thead><tr><th>When</th><th>Supervisor</th><th>Contributor ID</th><th>Prompt type</th><th>Recording</th><th>Decision</th><th>Production status</th><th>Notes</th></tr></thead><tbody>';
 foreach($rows as $r){
  $pid=(string)($r['participant_id']??'');$record=(int)$r['recording_id'];
  $participant=$pid!==''?'<a href="'.h(linkto('contributor',['id'=>$pid])).'">'.h($pid).'</a>':'Unavailable';
  $label=match($r['decision']){'approve'=>'Approve','reject'=>'Reject','flag'=>'Note only','escalate'=>'Historical escalation',default=>ucfirst((string)$r['decision'])};
  $production=$r['production_state']??null;
  $html.='<tr><td>'.h(date_ml($r['created_at'])).'</td><td>'.h($r['display_name']).'</td><td>'.$participant.'</td><td>'.h(audit_prompt_name($r['prompt_type']??null)).'</td><td><a href="'.h(linkto('voice',['id'=>$record])).'">#'.$record.'</a></td><td>'.h($label).'</td><td>'.h($production!==null?ucfirst((string)$production):'Not recorded').'</td><td>'.h($r['notes']??'').'</td></tr>';
 }
 if(!$rows)$html.='<tr><td colspan="8" class="no-data">No review submissions match these filters.</td></tr>';
 $html.='</tbody></table></div>'.pagination($page,$total,$perPage,array_merge(['page'=>'audit','tab'=>'reviews'],$filtered)).'</section>';
 $html.='<div class="audit-analytics">'.audit_review_breakdown_table('Totals by supervisor',$bySupervisor).audit_review_breakdown_table('Totals by prompt type',$byPrompt).audit_review_breakdown_table('Totals by contributor',$byContributor).'</div>';
 return $html;
}

function profile_page(array $u): string {
 $display=h($u['display_name']);$email=h($u['email']);$role=ucfirst((string)$u['role']);
 $hasPhoto=profile_photo_available($u);
 $avatar=avatar_markup($u,'portrait');
 $html=page_top('Profile & security','Manage your identity, profile photo and password.','MY ACCOUNT');
 $html.='<div class="profile-layout"><div class="profile-side">';
 $html.='<section class="panel profile-identity"><div class="identity-avatar">'.$avatar.'</div><h2>'.$display.'</h2><p>'.h($email).' <span class="pill accepted">'.h($role).'</span></p><div class="identity-line"><span>Account</span><strong>'.((int)$u['active']?'Active':'Inactive').'</strong></div><div class="identity-line"><span>Last login</span><strong>'.h(date_ml($u['last_login_at']??null)).'</strong></div></section>';
 $html.='<section class="panel profile-photo-panel"><h2>Profile picture</h2><p>JPG, PNG or WebP · maximum 2 MB. Crop to a portrait before saving.</p>';
 $html.='<form method="post" action="'.h(linkto('profile_photo')).'" enctype="multipart/form-data" id="profile-photo-form" class="stack">'.csrf_input();
 $html.='<label class="file-label">Choose a photo to crop<input type="file" id="profile-photo-input" name="profile_photo" accept="image/jpeg,image/png,image/webp" required></label>';
 $html.='<div class="crop-box" id="photo-crop-box" hidden><div class="crop-stage"><canvas id="profile-crop-canvas" width="350" height="450" aria-label="Interactive photo crop preview"></canvas></div><label class="crop-zoom">Zoom <input type="range" id="photo-zoom" min="100" max="300" step="5" value="100"></label><p class="hint">Drag the image to position your face, then adjust the zoom.</p></div>';
 $html.='<div id="photo-feedback" class="hint" role="status" aria-live="polite"></div>';
 $html.='<button class="btn primary wide" type="submit">Crop & save photo</button></form>';
 $html.='<form method="post" action="'.h(linkto('remove_profile_photo')).'" class="photo-remove-form">'.csrf_input().'<button class="btn outline wide" type="submit"'.(!$hasPhoto?' disabled':'').'>Remove photo</button></form>';
 $html.='<p class="hint">Photos are stored privately on this hosting account and shown only to authenticated users. Your photo is independent of production audio.</p></section></div>';
 $html.='<div class="profile-main"><section class="panel profile-form-panel"><h2>Personal details</h2><p>Shown in your dashboard and the review audit log.</p>';
 $html.='<form method="post" action="'.h(linkto('update_profile')).'" class="stack">'.csrf_input();
 $html.='<label>Email address<input type="email" value="'.$email.'" readonly title="Your sign-in email cannot be changed from this page"></label>';
 $html.='<label>Display name<input type="text" name="name" value="'.$display.'" maxlength="120" minlength="2" autocomplete="name" required></label><button class="btn primary wide">Save profile</button></form></section>';
 $html.='<section id="change-password" class="panel profile-form-panel"><div class="profile-panel-heading"><h2>Change password</h2><span class="pill">Secure account</span></div><p>Use a unique password of at least 12 characters. All other sessions will need to sign in again after a change.</p>';
 $html.='<form method="post" action="'.h(linkto('change_password')).'" class="stack">'.csrf_input();
 $html.='<label>Current password<div class="password-input"><input id="password-current" name="current_password" type="password" autocomplete="current-password" required><button type="button" class="password-toggle" data-password-toggle="password-current" aria-label="Show current password">Show</button></div></label>';
 $html.='<label>New password<div class="password-input"><input id="password-new" name="new_password" type="password" autocomplete="new-password" minlength="12" maxlength="128" required><button type="button" class="password-toggle" data-password-toggle="password-new" aria-label="Show new password">Show</button></div></label>';
 $html.='<label>Confirm new password<div class="password-input"><input id="password-confirm" name="confirm_password" type="password" autocomplete="new-password" minlength="12" maxlength="128" required><button type="button" class="password-toggle" data-password-toggle="password-confirm" aria-label="Show confirmation password">Show</button></div></label>';
 $html.='<button class="btn primary wide" type="submit">Update password</button></form></section>';
 $html.='<section class="panel profile-signout"><form method="post" action="'.h(linkto('logout')).'">'.csrf_input().'<button type="submit" class="btn outline">Sign out of this account</button></form></section></div></div>';
 return $html;
}
function feedback_page(array $u): string {
 require_admin();
 $write=official_write_status();
 $html=page_top('Feedback delivery','Manage the production messaging connection and track feedback readiness.');
 $html.='<div class="metrics three">'.box_metric('Production QC link',$write==='ready'?'Ready':'Setup required','Direct supervisor QC save').box_metric('WhatsApp messaging','Setup required','Only with recorded participant consent').box_metric('Delivery receipts','Pending','Provider webhook connection').'</div>';
 $html.=panel_start('Messaging connection','Messages can be sent only after the authorized provider has been connected.');
 $html.='<div class="checklist"><div>Production QC write-back <b>'.h($write==='ready'?'Connected':'Needs configuration').'</b></div><div>Messaging provider & approved templates <b>Not connected</b></div><div>Verified participant consent <b>Requires source validation</b></div><div>Delivery reporting <b>Pending provider webhooks</b></div></div>';
 $html.='<details class="help-disclosure"><summary><span class="help-circle">?</span> What is required to enable WhatsApp feedback?</summary><p>An authorized DigitalOcean messaging service must check each contributor’s opt-in, select a Meta-approved template when needed, use the existing provider credential privately, and track sent, delivered and failed statuses. The public dashboard must not store phone numbers or WhatsApp credentials. Until this service is configured, no messages are sent.</p></details></section>';
 return $html;
}
function integration_page(array $u): string {
 require_admin();$last=sync_timestamp();$connected=sync_ready();$write=official_write_status();
 // Check that at least one known stored clip can be read by the owner-approved audio bridge.
 $audio='Pending check';
 if($connected){$sample=query("SELECT id FROM recordings WHERE file_status='stored' ORDER BY id DESC LIMIT 1")->fetchColumn();
  if($sample){try{[$raw]=production_fetch('/v1/supervision/audio/'.(int)$sample.'/status');$status=json_decode($raw,true);$audio=!empty($status['available'])?'Verified':'Not available';}catch(Throwable){$audio='Needs attention';}}
 }
 $html=page_top('Production connection','Monitor synchronized recordings, private playback and confirmed production actions.','ADMINISTRATION · SYSTEM STATUS');
 $html.='<div class="metrics three">'.box_metric('HTTPS production connection',$connected?'Connected':'Not configured','Secure DigitalOcean gateway').box_metric('Last successful synchronization',$last?date_ml($last):'Never','Hostinger metadata refresh').box_metric('Accepted-hours calculation',setting('hours_verified','0')==='1'?'Verified':'Unavailable','500-hour accepted speech target').'</div>';
 $html.=panel_start('Connection checks','Production recordings stay on DigitalOcean. Each action uses authenticated access.');
 $html.='<div class="checklist"><div>Online portal & accounts <b>Active</b></div><div>Recording metadata <b>'.($last?'Synchronized':'Not synchronized').'</b></div><div>Private audio playback <b>'.h($audio).'</b></div><div>Production QC write-back <b>'.h($write==='ready'?'Ready':($write==='disabled'?'Not activated':'Needs attention')).'</b></div><div>Automatic updates <b>Checks every 5 seconds while portal is open</b></div><div>Background refresh <b>Use Hostinger scheduled task</b></div></div>';
 $html.='<details class="help-disclosure"><summary><span class="help-circle">?</span> What do these statuses mean?</summary><p>Metadata sync brings authorized recording details from DigitalOcean to this online portal. Private audio is streamed on demand. Official QC requires a separate production-approved, audited endpoint; the gateway confirms each action before its status is displayed as final. Hostinger scheduled tasks handle unattended refresh.</p></details></section>';
 $html.=panel_start('Synchronize now','Check for the latest production metadata without modifying original recordings.');
 $html.='<form method="post" action="'.h(linkto('sync')).'">'.csrf_input().'<button type="submit" class="btn primary" '.(!$connected?'disabled':'').'>'.svg_icon('sync').' Sync now</button></form></section>';
 return $html;
}

function settings_page(array $u): string {
 require_admin();
 $period=(int)setting('refresh_seconds','5');if($period<5||$period>60)$period=5;
 $html=page_top('System settings','Review active online configuration and controls for synchronization and supervisor access.','ADMINISTRATION · CONFIGURATION');
 $html.='<div class="metrics three">'.box_metric('Portal mode','Online','Hosted at Hostinger').box_metric('Production authority','DigitalOcean','QC state and original audio').box_metric('Speech target','500 hours','Eligible accepted audio only').'</div>';
 $html.=panel_start('Live updates','Browser refresh checks are coordinated server-side. Background updates use the Hostinger scheduled task.');
 $html.='<form method="post" action="'.h(linkto('save_settings')).'" class="settings-form">'.csrf_input().'<label>Live synchronization check interval<select name="refresh_seconds">'.select_options(['5'=>'Every 5 seconds','10'=>'Every 10 seconds','15'=>'Every 15 seconds','30'=>'Every 30 seconds','60'=>'Every 60 seconds'],(string)$period).'</select></label><button class="btn primary">Save settings</button></form>';
 $html.='<details class="help-disclosure"><summary><span class="help-circle">?</span> Background synchronization</summary><p>Live checks occur while the browser is open. To refresh unattended, configure the Hostinger cron task for <code>cron/sync.php</code> at a supported interval. The website cannot guarantee background execution every five seconds on shared hosting.</p></details></section>';
 $html.=panel_start('Access and decision protection','These safeguards remain enforced on the server, not only by the interface.');
 $html.='<div class="checklist"><div>Admin visibility <b>All contributors and accounts</b></div><div>Supervisor visibility <b>Assigned contributors only</b></div><div>Private audio playback <b>Checked for each recording</b></div><div>Supervisor decisions <b>Final once securely saved — no second reviewer</b></div><div>Audit history <b>Append-only review attempts</b></div><div>Version <b>0.0.0.1</b></div></div></section>';
 return $html;
}
