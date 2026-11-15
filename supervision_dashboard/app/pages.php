<?php
declare(strict_types=1);
function get_str(string $name,string $default=''): string {return mbcut(trim((string)($_GET[$name]??$default)),120);}
function date_filter_sql(string $from,string $to,string $alias='r'): array {
 $w='';$args=[];
 if($from!=='') {[$s]=ml_day_bounds($from);$w.=' AND '.$alias.'.received_at >= ?';$args[]=$s;}
 if($to!=='') {[, $end]=ml_day_bounds($to);$w.=' AND '.$alias.'.received_at < ?';$args[]=$end;}
 return [$w,$args];
}
function records_conditions(array $u,string $alias='r'): array {
 $terms=[];$params=[];
 [$scope,$scopeArgs]=scope_sql($u,$alias);$terms[]=$scope;$params=array_merge($params,$scopeArgs);
 $search=get_str('search');if($search!==''){
   if(ctype_digit($search)){$terms[]=" AND ($alias.id=? OR $alias.participant_id LIKE ?)";$params[]=(int)$search;$params[]='%'.$search.'%';}
   else{$terms[]=" AND $alias.participant_id LIKE ?";$params[]='%'.$search.'%';}
 }
 $qc=get_str('qc');if(in_array($qc,['accepted','pending','needs_review','rejected'],true)){$terms[]=" AND $alias.qc_status=?";$params[]=$qc;}
 $prompt=get_str('prompt');
 if(in_array($prompt,['sentence','image','free_speech','conversation_starter'],true)){
   $terms[]=" AND LOWER(COALESCE($alias.prompt_type,'')) = ?";
   $params[]=$prompt;
 }elseif($prompt==='unclassified'){
   $terms[]=" AND ($alias.prompt_type IS NULL OR LOWER($alias.prompt_type) NOT IN ('sentence','image','free_speech','conversation_starter'))";
 }
 $day=get_str('day');$from=get_str('from',$day);$to=get_str('to',$day);
 [$dateSql,$dateArgs]=date_filter_sql($from,$to,$alias);$terms[]=$dateSql;$params=array_merge($params,$dateArgs);
 $state=get_str('state');
 $human=get_str('human');
 if(in_array($human,['accept','reject'],true)){$terms[]=' AND '.$alias.'.qc_tier3=?';$params[]=$human;}
 elseif($human==='pending')$terms[]=' AND '.$alias.'.qc_tier3 IS NULL';
 if($state==='open')$terms[]=' AND '.$alias.'.qc_tier3 IS NULL';
 elseif($state==='reviewed')$terms[]=' AND '.$alias.'.qc_tier3 IS NOT NULL';
 if(get_str('type')==='real')$terms[]=" AND $alias.is_test=0";elseif(get_str('type')==='test')$terms[]=" AND $alias.is_test=1";
 return [implode('',$terms),$params];
}
function record_listing(array $u,string $mode='voices'): string {
 $isReview=in_array($mode,['review','queue'],true);$isNotifications=$mode==='notifications';$state=get_str('state');
 [$where,$params]=records_conditions($u);
 if($isNotifications && $state==='unseen'){$where.=' AND r.id > COALESCE((SELECT last_seen_recording_id FROM notification_state WHERE user_id=?),0)';$params[]=(int)$u['id'];}
 if($isNotifications && $state==='seen'){$where.=' AND r.id <= COALESCE((SELECT last_seen_recording_id FROM notification_state WHERE user_id=?),0)';$params[]=(int)$u['id'];}
 $per=25;$page=max(1,min(10000,(int)($_GET['p']??1)));$offset=($page-1)*$per;
 $total=count_value('SELECT COUNT(*) FROM recordings r WHERE 1=1'.$where,$params);
 if($isReview && get_str('state')==='' && get_str('human')===''){$where.=' AND r.qc_tier3 IS NULL';$total=count_value('SELECT COUNT(*) FROM recordings r WHERE 1=1'.$where,$params);}
 $sort=$isReview?"CASE r.qc_status WHEN 'needs_review' THEN 0 WHEN 'pending' THEN 1 WHEN 'rejected' THEN 2 ELSE 3 END, r.received_at DESC":"r.received_at DESC";
 $sql="SELECT r.*, (SELECT CONCAT(UPPER(LEFT(rd.decision,1)),SUBSTRING(rd.decision,2),' · ',rd.production_state) FROM review_drafts rd WHERE rd.recording_id=r.id ORDER BY rd.id DESC LIMIT 1) AS draft_state FROM recordings r WHERE 1=1 $where ORDER BY $sort LIMIT $per OFFSET $offset";
 $rows=query($sql,$params)->fetchAll();
 $head=match($mode){'review'=>'Production review workspace','queue'=>'Review queue','notifications'=>'New recordings & notifications','recordings'=>'Recording history',default=>'Production voices'};
 $desc=$isReview?'Prioritized recordings awaiting a final supervisor decision; arrival QC is shown separately.':($isNotifications?'Incoming production metadata synchronized from the authorized collector.':'Search recording metadata, filter by Malawi dates and open a recording to listen and review.');
 $html=page_top($head,$desc);
 $html.='<div class="panel filters"><form method="get" class="filter-grid"><input type="hidden" name="page" value="'.h($mode).'">';
 $html.='<label>Contributor or recording ID<input type="search" name="search" value="'.h(get_str('search')).'" placeholder="P0020 or 183"></label>';
 $html.='<label>From date (Malawi)<input type="date" name="from" value="'.h(get_str('from',get_str('day'))).'"></label>';
 $html.='<label>To date<input type="date" name="to" value="'.h(get_str('to',get_str('day'))).'"></label>';
 $html.='<label>Arrival QC<select name="qc">'.select_options([''=>'All QC statuses','needs_review'=>'Needs human review','pending'=>'Pending','rejected'=>'Rejected','accepted'=>'Accepted'],get_str('qc')).'</select></label>';
 $html.='<label>Prompt type<select name="prompt">'.select_options([''=>'All prompt types','sentence'=>'Reading sentences','image'=>'Pictures','free_speech'=>'Free speech','conversation_starter'=>'Conversation starter','unclassified'=>'Unclassified / unassigned'],get_str('prompt')).'</select></label>';
 $html.='<label>Tier 3 supervisor decision<select name="human">'.select_options([''=>'All decisions','pending'=>'Not reviewed','accept'=>'Accepted','reject'=>'Rejected'],get_str('human')).'</select></label>';
 $html.='<label>Review / notification state<select name="state">'.select_options($isNotifications?[''=>'All notifications','unseen'=>'Unseen','seen'=>'Seen']:[''=>'All recordings','open'=>'Not reviewed','reviewed'=>'Reviewed'],get_str('state')).'</select></label>';
 $html.='<div class="form-buttons"><button class="btn primary" type="submit">Load recordings</button><a class="btn outline" href="'.h(linkto($mode)).'">Clear</a></div></form></div>';
 if($mode==='recordings') $html.=record_calendar($u);
 if($isNotifications){$seen=count_value('SELECT COUNT(*) FROM recordings r WHERE r.id>COALESCE((SELECT last_seen_recording_id FROM notification_state WHERE user_id=?),0)'.scope_sql($u)[0],array_merge([(int)$u['id']],scope_sql($u)[1]));$html.='<div class="metrics two">'.box_metric('Unseen recordings',(string)$seen,'Per-user notification cursor','notifications',['state'=>'unseen']).box_metric('Accessible production voices',(string)count_value('SELECT COUNT(*) FROM recordings r WHERE 1=1'.scope_sql($u)[0],scope_sql($u)[1]),'Assigned contributor recordings','voices').'</div>';
 $html.='<form method="post" action="'.h(linkto('mark_seen')).'" class="inline-form">'.csrf_input().'<button class="btn outline" type="submit">Mark all available recordings as seen</button></form>';
 }
 $html.=panel_start('Recordings','Matched: '.number_format($total).' · 25 per page · Times displayed in Malawi local time');
 $html.='<div class="table-scroll"><table><thead><tr><th>Recording</th><th>Contributor</th><th>Received (Malawi)</th><th>Duration</th><th>Prompt type</th><th>Arrival QC</th><th>Supervisor review</th><th>Action</th></tr></thead><tbody>';
 foreach($rows as $r){$html.='<tr><td><b>#'.h($r['id']).'</b></td><td><a href="'.h(linkto('contributor',['id'=>$r['participant_id']])).'">'.h($r['participant_id']).'</a></td><td>'.h(date_ml($r['received_at'])).'</td><td>'.h(duration($r['duration_ms']===null?null:(int)$r['duration_ms'])).'</td><td>'.h(prompt_label($r['prompt_type']??null)).'</td><td><span class="pill '.h($r['qc_status']).'">'.h(status_label($r['qc_status'])).'</span></td><td>'.h(match($r['qc_tier3']??null){'accept'=>'Accepted','reject'=>'Rejected',default=>'Not yet reviewed'}).'</td><td><a class="btn small outline" href="'.h(linkto('voice',['id'=>$r['id']])).'">Listen & review →</a></td></tr>';}
 if(!$rows)$html.='<tr><td colspan="8" class="no-data">No synchronized recordings match these filters. Verify synchronization or change the date/QC filters.</td></tr>';
 $html.='</tbody></table></div>'.pagination($page,$total,$per,array_filter(['page'=>$mode,'search'=>get_str('search'),'from'=>get_str('from',get_str('day')),'to'=>get_str('to',get_str('day')),'qc'=>get_str('qc'),'prompt'=>get_str('prompt'),'state'=>$state,'human'=>get_str('human')])).'</section>';
 return $html;
}
function select_options(array $options,string $selected): string { $html='';foreach($options as $value=>$label)$html.='<option value="'.h((string)$value).'"'.((string)$value===$selected?' selected':'').'>'.h($label).'</option>';return $html; }
function record_calendar(array $u): string {
 $month=get_str('month',substr(ml_today(),0,7));if(!preg_match('/^\d{4}-(0[1-9]|1[0-2])$/',$month))$month=substr(ml_today(),0,7);
 $first=new DateTimeImmutable($month.'-01',new DateTimeZone(APP_TIMEZONE));$days=(int)$first->format('t');
 [$a,$b]=date_filter_sql($month.'-01',$first->modify('+1 month')->modify('-1 day')->format('Y-m-d'));
 [$scope,$args]=scope_sql($u);
 $sql="SELECT DATE(DATE_ADD(r.received_at,INTERVAL 2 HOUR)) AS local_day,COUNT(*) AS n FROM recordings r WHERE 1=1 $scope $a GROUP BY local_day ORDER BY local_day";
 $counts=[];foreach(query($sql,array_merge($args,$b))->fetchAll() as $r)$counts[$r['local_day']]=(int)$r['n'];
 $html=panel_start('Recording dates · '.$first->format('F Y'),'Click a date to display that day’s production recordings. Zero means no entries in the synchronized catalog.');
 $html.='<div class="calendar-bar"><a class="btn outline small" href="'.h(linkto('recordings',['month'=>$first->modify('-1 month')->format('Y-m')])).'">← Previous</a><b>'.h($first->format('F Y')).'</b><a class="btn outline small" href="'.h(linkto('recordings',['month'=>$first->modify('+1 month')->format('Y-m')])).'">Next →</a></div><div class="daygrid">';
 for($d=1;$d<=$days;$d++){$date=$month.'-'.str_pad((string)$d,2,'0',STR_PAD_LEFT);$count=$counts[$date]??0;$selected=get_str('day')===$date?' chosen':'';$html.='<a class="daycell'.$selected.'" href="'.h(linkto('recordings',['month'=>$month,'day'=>$date])).'"><span>'.$d.'</span><strong>'.$count.'</strong></a>';}
 return $html.'</div></section>';
}

function prompt_group(?string $type): string {
 $key=strtolower(trim((string)$type));
 return in_array($key,['sentence','image','free_speech','conversation_starter'],true)?$key:'unclassified';
}
function prompt_label(?string $type): string {
 return match(prompt_group($type)){
  'sentence'=>'Reading sentences','image'=>'Pictures','free_speech'=>'Free speech',
  'conversation_starter'=>'Conversation starter',default=>'Unclassified / unassigned'
 };
}
/** Only stored, measured, real, non-duplicate recording duration counts in type charts. */
function prompt_chart_data(array $u): array {
 [$scope,$args]=scope_sql($u);
 $out=[];
 foreach(['sentence','image','free_speech','conversation_starter','unclassified'] as $key){
   $out[$key]=['recordings'=>0,'recorded_ms'=>0,'accepted_ms'=>0,'pending_ms'=>0,'review_ms'=>0,'rejected_ms'=>0];
 }
 $rows=query("SELECT r.prompt_type,r.qc_final,COALESCE(SUM(r.duration_ms),0) AS ms,COUNT(*) AS n
    FROM recordings r WHERE r.file_status='stored' AND r.is_test=0 AND r.duplicate_of IS NULL
    AND r.duration_ms IS NOT NULL AND r.duration_ms>0 $scope
    GROUP BY r.prompt_type,r.qc_final",$args)->fetchAll();
 foreach($rows as $row){
  $key=prompt_group($row['prompt_type']??null);
  $ms=(int)$row['ms']; $n=(int)$row['n'];
  $out[$key]['recorded_ms']+=$ms;
  $out[$key]['recordings']+=$n;
  $status=match((string)$row['qc_final']){
   'gold'=>'accepted_ms','silver'=>'accepted_ms','rejected'=>'rejected_ms',default=>'pending_ms'
  };
  if($status!==null)$out[$key][$status]+=$ms;
 }
 return $out;
}
function prompt_hours(int $ms): string {return number_format($ms/3600000,3,'.',',');}
function prompt_ring(int $numerator,int $denominator,string $color='#16b69a'): string {
 $percent=$denominator>0?min(100,max(0,100*$numerator/$denominator)):0.0;
 $stop=number_format($percent,4,'.','');
 return 'background:conic-gradient('.$color.' 0% '.$stop.'%, #e8e9f8 '.$stop.'% 100%)';
}
/**
 * Four colored donuts show RECORDED AUDIO share across classified prompt types.
 * The companion breakdown shows QC-ACCEPTED duration by category, not recorded mix.
 * The separate 500-hour target remains ACCEPTED ONLY.
 */
function prompt_type_pies(array $u): string {
 $data=prompt_chart_data($u);
 $types=[
  'sentence'=>['Reading sentences','#6d58e8'],
  'image'=>['Pictures','#43baa2'],
  'free_speech'=>['Free speech','#5b9ff5'],
  'conversation_starter'=>['Conversation starter','#efae4e']
 ];
 $known=0;$knownAccepted=0;
 foreach($types as $key=>$item){$known+=$data[$key]['recorded_ms'];$knownAccepted+=$data[$key]['accepted_ms'];}
 $missing=$data['unclassified'];$meta=setting('prompt_metadata_available','0')==='1';
 $html='<section class="panel prompt-panel"><div class="panel-head"><div><div class="eyebrow">SOURCE CLASSIFICATION · REAL RECORDINGS</div><h2>Recording progress by prompt type</h2><p>Each colored ring represents recorded duration for one prompt type. The usable-hours summary counts only gold/silver final QC; arrival and human QC are separate.</p></div></div>';
 if(!$meta){
  $html.='<div class="notice warning">Prompt types are not currently available from the production gateway. The chart does not guess classifications.</div>';
 }elseif($missing['recordings']>0){
  $html.='<div class="notice warning">'.number_format($missing['recordings']).' stored recording(s) have no matching assignment type and are excluded from the four-category comparison until verified.</div>';
 }
 $html.='<div class="prompt-category-grid">';
 foreach($types as $type=>$item){
  [$name,$color]=$item;$r=$data[$type];
  $recorded=$r['recorded_ms'];$accepted=$r['accepted_ms'];$unaccepted=max(0,$recorded-$accepted);
  $portion=$known>0?100*$recorded/$known:0.0;
  $html.='<a class="prompt-category-card" href="'.h(linkto('voices',['prompt'=>$type])).'" aria-label="Open '.h($name).' recordings">';
  $html.='<div class="prompt-category-heading"><span class="prompt-type-heading">'.h($name).'</span><span aria-hidden="true">↗</span></div>';
  $html.='<div class="prompt-ring" style="'.h(prompt_ring($recorded,$known,$color)).'" role="img" aria-label="'.h($name).': '.number_format($portion,1).' percent of classified recorded duration">';
  $html.='<div class="prompt-ring-inner"><b>'.number_format($portion,1).'%</b><small>of total recorded</small></div></div>';
  $html.='<div class="prompt-kpi"><span>Recorded</span><b>'.prompt_hours($recorded).' h</b></div>';
  $html.='<div class="prompt-kpi"><span>Usable (gold/silver)</span><b>'.(setting('qc_final_available','0')==='1'?prompt_hours($accepted).' h':'—').'</b></div>';
  $html.='<div class="prompt-kpi"><span>Bronze/rejected/unverified</span><b>'.(setting('qc_final_available','0')==='1'?prompt_hours($unaccepted).' h':'—').'</b></div>';
  $html.='<div class="prompt-small">'.number_format($r['recordings']).' eligible recorded clips · View recordings</div></a>';
 }
 $html.='</div>';
 $verified=setting('qc_final_available','0')==='1';
 $acceptedMs=$verified?(int)setting('usable_ms','0'):0;
 $targetMs=(int)(APP_TARGET_HOURS*3600000);
 $remainingMs=$verified?max(0,$targetMs-$acceptedMs):0;
 $targetPercent=$verified?min(100,100*$acceptedMs/$targetMs):0;
 $html.='<div class="prompt-aggregate"><div class="prompt-aggregate-main"><div class="prompt-aggregate-title"><span class="eyebrow">ALL FOUR TYPES · PROJECT TOTAL</span><h3>500-hour usable-audio target</h3><p>Only final gold/silver audio counts toward the 500-hour usable target. The four prompt cards separately show classified, real, non-duplicate recordings.</p></div>';
 $html.='<div class="prompt-aggregate-row"><div class="prompt-ring prompt-ring-large" style="'.h($verified?prompt_ring($acceptedMs,$targetMs):'background:#e8e9f8;').'" role="img" aria-label="'.h($verified?'Usable goal completion '.number_format($targetPercent,2).' percent':'Final quality labels have not yet been verified').'"><div class="prompt-ring-inner"><b>'.($verified?number_format($targetPercent,2).'%':'—').'</b><small>of 500 hours</small></div></div>';
 $html.='<div class="prompt-aggregate-stats"><div><small>Usable gold/silver (all types)</small><strong>'.($verified?prompt_hours($acceptedMs).' h':'—').'</strong></div><div><small>Remaining to 500-hour goal</small><strong>'.($verified?prompt_hours($remainingMs).' h':'—').'</strong></div><div><small>Recorded before QC (including unclassified)</small><strong>'.prompt_hours($known+$missing['recorded_ms']).' h</strong></div></div></div></div>';
 $html.='<div class="prompt-breakdown"><strong>Usable gold/silver duration by prompt type</strong><p class="prompt-small">Share of independently usable eligible duration. Unreviewed and rejected recordings are not included.</p>';
 foreach($types as $key=>$item){
  [$name,$color]=$item;
  $typeAccepted=$data[$key]['accepted_ms'];
  $portion=$knownAccepted>0?100*$typeAccepted/$knownAccepted:0;
  $html.='<a class="prompt-breakdown-row" href="'.h(linkto('voices',['prompt'=>$key,'state'=>'reviewed'])).'"><span>'.h($name).' · '.prompt_hours($typeAccepted).' h</span><b>'.number_format($portion,1).'%</b><div class="prompt-mini-bar"><div style="background:'.h($color).';width:'.number_format($portion,3,'.','').'%"></div></div></a>';
 }
 $html.='<div class="prompt-accepted-total"><span>Usable classified audio</span><strong>'.prompt_hours($knownAccepted).' h</strong></div>';
 $html.='</div></div>';
 if($missing['recordings']>0){$html.='<p class="prompt-unclassified"><a href="'.h(linkto('voices',['prompt'=>'unclassified'])).'">'.number_format($missing['recordings']).' unclassified / unassigned clips ('.prompt_hours($missing['recorded_ms']).' h) — investigate source links</a></p>';}
 return $html.'</section>';
}

function hours_info(): array {
 $verified=setting('qc_final_available','0')==='1';
 $usable=$verified?(int)setting('usable_ms','0'):null;
 $hours=$usable===null?null:$usable/3600000;
 $pct=$hours===null?0:min(100,max(0,$hours/APP_TARGET_HOURS*100));
 return ['verified'=>$verified,'accepted'=>$usable,'hours'=>$hours,'pct'=>$pct,'stored'=>setting('stored'),'last'=>sync_timestamp()];
}
function progress_panel(): string {
 $a=hours_info();$pct=$a['verified']?number_format((float)$a['pct'],2):'—';
 $usable=$a['verified']?number_format((float)$a['hours'],3):'—';
 $collected=setting('collected_ms');$collectedText=$collected!==null?number_format((int)$collected/60000,2).' min':'—';
 $bronze=setting('bronze_ms');$rejected=setting('rejected_final_ms');
 $html='<section class="panel progress-panel"><div class="eyebrow">DUNSTAN QUALITY CONTRACT · DISTINCT METRICS</div><div class="panel-head"><div><h2>Collected versus usable speech</h2><p>Collected follows bot Tier 1 (stored audio not rejected on arrival). Usable follows final gold or silver labels. Bronze and rejected are not usable.</p></div></div>';
 $html.='<div class="metrics four">'.box_metric('Collected minutes',$collectedText,'Tier 1, not rejected on arrival').box_metric('Usable minutes',$a['verified']?number_format((int)setting('usable_ms','0')/60000,2).' min':'—','Final QC: gold and silver').box_metric('Bronze minutes',$a['verified']?number_format((int)$bronze/60000,2).' min':'—','Final QC: bronze').box_metric('Rejected minutes',$a['verified']?number_format((int)$rejected/60000,2).' min':'—','Final QC: rejected').'</div>';
 $html.='<div class="ring-layout"><div class="progress-ring" role="progressbar" aria-label="500-hour usable audio goal" aria-valuemin="0" aria-valuemax="500" aria-valuenow="'.h((string)($a['hours']??0)).'" style="--pct:'.number_format((float)$a['pct'],3,'.','').'%"><div class="ring-inside"><strong>'.h($pct).($a['verified']?'%':'').'</strong><small>usable goal</small></div></div><div class="ring-stat"><span>Usable audio toward 500-hour target</span><strong>'.h($usable).' <small>/ 500 h</small></strong><small>'.($a['verified']?'Verified against DigitalOcean final QC labels':'Final QC not available; unknown, not zero').'</small></div></div>';
 $html.='<div class="goalbar"><div style="width:'.h((string)$a['pct']).'%"></div></div><p class="tiny">Last successful metadata sync: '.h($a['last']?date_ml($a['last']):'None').'. QC fields are read-only on Hostinger.</p></section>';
 return $html;
}
function overview_page(array $u): string {
 [$sc,$arg]=scope_sql($u);
 $tot=count_value('SELECT COUNT(*) FROM recordings r WHERE 1=1'.$sc,$arg);
 $pending=count_value("SELECT COUNT(*) FROM recordings r WHERE r.qc_status='pending'".$sc,$arg);
 $need=count_value("SELECT COUNT(*) FROM recordings r WHERE r.qc_status='needs_review'".$sc,$arg);
 $rejected=count_value("SELECT COUNT(*) FROM recordings r WHERE r.qc_status='rejected'".$sc,$arg);
 $accepted=count_value("SELECT COUNT(*) FROM recordings r WHERE r.qc_status='accepted'".$sc,$arg);
 $drafts=count_value('SELECT COUNT(*) FROM review_drafts d JOIN recordings r ON r.id=d.recording_id WHERE 1=1'.$sc,$arg);
 $humanApproved=count_value("SELECT COUNT(*) FROM recordings r WHERE r.qc_tier3='accept'".$sc,$arg);
 $humanRejected=count_value("SELECT COUNT(*) FROM recordings r WHERE r.qc_tier3='reject'".$sc,$arg);
 $html=page_top('Overview','Collection quality, collected and usable speech progress and authorized production activity.');
 $html.=prompt_type_pies($u);
 // Project-wide, de-identified demographic aggregates are admin-only.
 if($u['role']==='admin') {
  $html.='<section id="masakhane-dashboard-demographics" class="mdemo-v8" aria-label="Contributor demographics"><div class="mdemo-v8-header"><p class="mdemo-v8-kicker">CONTRIBUTOR DEMOGRAPHICS</p><h2 class="mdemo-v8-title">Recorded voices by contributor characteristics</h2><p class="mdemo-v8-subtitle">Project-wide recorded hours by gender, country, age group and first language. Small groups are combined for privacy.</p></div><div class="mdemo-v8-grid"><p class="mdemo-v8-info">Loading contributor breakdowns…</p></div></section>';
 }
 $html.='<div class="metrics four">'.box_metric('QC pending',(string)$pending,'Awaiting arrival checks','voices',['qc'=>'pending']).box_metric('Needs human review',(string)$need,'Flagged by production QC','queue',['qc'=>'needs_review']).box_metric('QC rejected',(string)$rejected,'Rejected on arrival','voices',['qc'=>'rejected']).box_metric('QC accepted',(string)$accepted,'Accepted on arrival (Tier 1)','voices',['qc'=>'accepted']).'</div>';
 $html.='<div class="metrics two">'.box_metric('Tier 3 accepted recordings',(string)$humanApproved,'Confirmed independent human decisions','voices',['human'=>'accept']).box_metric('Tier 3 rejected recordings',(string)$humanRejected,'Confirmed independent human decisions','voices',['human'=>'reject']).'</div>';
 $html.='<div class="metrics three">'.box_metric('Visible recordings',(string)$tot,'Your authorized production catalog','voices').box_metric('Review submissions',(string)$drafts,'Online audit · confirmation status shown per review','review',['state'=>'reviewed']).box_metric('Follow-ups', (string)count_value('SELECT COUNT(*) FROM followups f JOIN recordings r ON r.id=f.recording_id WHERE 1=1'.$sc,$arg),'Online follow-up entries','followups').'</div>';
 $html.=panel_start('Review priorities','Production QC flags and confirmed supervisor decisions are shown separately.');
 $html.='<div class="action-tiles"><a href="'.h(linkto('queue',['qc'=>'needs_review'])).'">'.svg_icon('shield').'<strong>Needs review</strong><span>'.h((string)$need).' flagged recordings</span></a><a href="'.h(linkto('notifications')).'">'.svg_icon('bell').'<strong>New recordings</strong><span>Check recent uploads and unread items</span></a><a href="'.h(linkto('reports',['day'=>ml_today()])).'">'.svg_icon('chart').'<strong>Daily reports</strong><span>Malawi dates and recording totals</span></a></div></section>';
 return $html;
}
function production_page(array $u): string {
 [$where,$params]=scope_sql($u);$counts=[];
 foreach(['pending','needs_review','rejected','accepted'] as $st)$counts[$st]=count_value('SELECT COUNT(*) FROM recordings r WHERE r.qc_status=?'.$where,array_merge([$st],$params));
 return page_top('Production snapshot','Production quality statuses. Recording access is restricted to your assigned contributors.').progress_panel().'<div class="metrics four">'.box_metric('QC pending',(string)$counts['pending'],'Production QC','voices',['qc'=>'pending']).box_metric('Needs human review',(string)$counts['needs_review'],'Production QC','voices',['qc'=>'needs_review']).box_metric('QC rejected',(string)$counts['rejected'],'Production QC','voices',['qc'=>'rejected']).box_metric('QC accepted',(string)$counts['accepted'],'Production QC','voices',['qc'=>'accepted']).'</div>';
}
function contributors_page(array $u): string {
 $search=get_str('search');$type=get_str('type');$q=' WHERE 1=1';$args=[];
 if($search!==''){$q.=' AND c.participant_id LIKE ?';$args[]='%'.$search.'%';}
 if(in_array($type,['real','test'],true)){$q.=' AND c.contributor_type=?';$args[]=$type;}
 if($u['role']!=='admin'){$q.=' AND EXISTS (SELECT 1 FROM supervisor_assignments a WHERE a.participant_id=c.participant_id AND a.supervisor_id=?)';$args[]=(int)$u['id'];}
 $total=count_value('SELECT COUNT(*) FROM contributors c'.$q,$args);$page=max(1,(int)($_GET['p']??1));$per=25;$offset=($page-1)*$per;
 $rows=query("SELECT c.*, (SELECT COUNT(*) FROM recordings r WHERE r.participant_id=c.participant_id) AS n, (SELECT SUM(r.file_status='stored') FROM recordings r WHERE r.participant_id=c.participant_id) AS stored, (SELECT SUM(r.qc_status='pending') FROM recordings r WHERE r.participant_id=c.participant_id) AS pending, (SELECT SUM(r.qc_status='needs_review') FROM recordings r WHERE r.participant_id=c.participant_id) AS flagged, (SELECT MAX(r.received_at) FROM recordings r WHERE r.participant_id=c.participant_id) AS newest FROM contributors c $q ORDER BY c.participant_id LIMIT $per OFFSET $offset",$args)->fetchAll();
 $html=page_top('Production contributors','Search permitted contributor identifiers and recording counts. Private names and telephone numbers stay with the collector.');
 $html.='<div class="metrics three">'.box_metric('Visible contributor IDs',(string)$total,'Authorized scope','contributors').box_metric('Visible voice recordings',(string)count_value('SELECT COUNT(*) FROM recordings r WHERE 1=1'.scope_sql($u)[0],scope_sql($u)[1]),'Your authorized recordings','voices').box_metric('Project goal','500 hours','Usable gold/silver final-QC audio','production').'</div>';
 $html.='<div class="panel filters"><form method="get" class="filter-grid"><input type="hidden" name="page" value="contributors"><label>Contributor ID<input name="search" value="'.h($search).'" placeholder="P0020"></label><label>Contributor type<select name="type">'.select_options([''=>'All','real'=>'Real','test'=>'Test'],$type).'</select></label><button class="btn primary" type="submit">Search contributors</button><a class="btn outline" href="'.h(linkto('contributors')).'">Clear</a></form></div>';
 $html.=panel_start('Contributor list','Only authorized IDs and recording metadata are displayed.');
 $html.='<div class="table-scroll"><table><thead><tr><th>Contributor ID</th><th>Status</th><th>Type</th><th>Voices</th><th>Stored</th><th>QC pending</th><th>Needs review</th><th>Most recent (Malawi)</th><th></th></tr></thead><tbody>';
 foreach($rows as $r){$html.='<tr><td><a href="'.h(linkto('contributor',['id'=>$r['participant_id']])).'"><strong>'.h($r['participant_id']).'</strong></a></td><td>'.h(ucfirst($r['status'])).'</td><td><span class="pill">'.h(ucfirst($r['contributor_type'])).'</span></td><td>'.(int)$r['n'].'</td><td>'.(int)$r['stored'].'</td><td>'.(int)$r['pending'].'</td><td>'.(int)$r['flagged'].'</td><td>'.h(date_ml($r['newest'])).'</td><td><a class="btn small outline" href="'.h(linkto('contributor',['id'=>$r['participant_id']])).'">View details →</a></td></tr>';}
 if(!$rows)$html.='<tr><td colspan="9" class="no-data">No contributors match this search.</td></tr>';
 return $html.'</tbody></table></div>'.pagination($page,$total,$per,['page'=>'contributors','search'=>$search,'type'=>$type]).'</section>';
}
/** Each supervisor can see only assigned contributor recording metadata. */
function contributor_clock(int $ms): string {
 $seconds=intdiv(max(0,$ms),1000);
 return sprintf('%02d:%02d:%02d',intdiv($seconds,3600),intdiv($seconds%3600,60),$seconds%60);
}
function contributor_detail_page(array $u,string $pid): string {
 if(!preg_match('/^[A-Za-z0-9_-]{1,80}$/',$pid)){http_response_code(404);return page_top('Contributor unavailable');}
 $where='c.participant_id=?';$params=[$pid];
 if($u['role']!=='admin'){$where.=' AND EXISTS (SELECT 1 FROM supervisor_assignments sa WHERE sa.supervisor_id=? AND sa.participant_id=c.participant_id)';$params[]=(int)$u['id'];}
 $contributor=query('SELECT c.participant_id,c.status,c.contributor_type FROM contributors c WHERE '.$where,$params)->fetch();
 if(!$contributor){http_response_code(404);return page_top('Contributor unavailable','This contributor is not assigned to your account or does not exist.');}
 $data=query("SELECT COUNT(*) AS total, COALESCE(SUM(file_status='stored'),0) AS stored,
    COALESCE(SUM(qc_tier3='accept'),0) AS accepted,
    COALESCE(SUM(qc_tier3='reject'),0) AS rejected,
    COALESCE(SUM(qc_tier3 IS NULL),0) AS pending,
    COALESCE(SUM(qc_status='needs_review'),0) AS needs_review,
    COALESCE(SUM(CASE WHEN file_status='stored' AND duration_ms>0 THEN duration_ms ELSE 0 END),0) AS recorded_ms,
    COALESCE(SUM(CASE WHEN file_status='stored' AND qc_tier3='accept' AND is_test=0 AND duplicate_of IS NULL AND duration_ms>0 THEN duration_ms ELSE 0 END),0) AS accepted_ms,
    COALESCE(SUM(CASE WHEN file_status='stored' AND qc_tier3='reject' AND duration_ms>0 THEN duration_ms ELSE 0 END),0) AS rejected_ms
   FROM recordings WHERE participant_id=?",[$pid])->fetch();
 $html=page_top('Contributor '.$pid,'Recorded speech and quality decisions for this assigned contributor.','CONTRIBUTOR · PRODUCTION');
 $html.='<div class="metrics four">'.box_metric('All recordings',number_format((int)$data['total']),'Stored: '.number_format((int)$data['stored']),'voices',['search'=>$pid]).box_metric('Total recorded duration',contributor_clock((int)$data['recorded_ms']),prompt_hours((int)$data['recorded_ms']).' hours of stored audio').box_metric('Tier 3 accepted duration',contributor_clock((int)$data['accepted_ms']),prompt_hours((int)$data['accepted_ms']).' hours · Tier 3 accepted, real, non-duplicate').box_metric('Rejected duration',contributor_clock((int)$data['rejected_ms']),prompt_hours((int)$data['rejected_ms']).' hours rejected by supervisors').'</div>';
 $html.='<div class="metrics four">'.box_metric('Accepted',(string)(int)$data['accepted'],'Supervisor decisions','voices',['search'=>$pid,'human'=>'accept']).box_metric('Rejected',(string)(int)$data['rejected'],'Supervisor decisions','voices',['search'=>$pid,'human'=>'reject']).box_metric('Awaiting review',(string)(int)$data['pending'],'Awaiting supervisor decision','voices',['search'=>$pid,'human'=>'pending']).box_metric('Needs review',(string)(int)$data['needs_review'],'Flagged for QC','voices',['search'=>$pid,'qc'=>'needs_review']).'</div>';
 $html.=panel_start('Contributor recordings','Latest 25 recordings. Open any permitted recording to listen or review.');
 $recent=query('SELECT id,prompt_type,received_at,duration_ms,qc_status FROM recordings WHERE participant_id=? ORDER BY received_at DESC,id DESC LIMIT 25',[$pid])->fetchAll();
 $html.='<div class="table-scroll"><table><thead><tr><th>Recording</th><th>Prompt type</th><th>Received (Malawi)</th><th>Duration</th><th>QC status</th><th>Action</th></tr></thead><tbody>';
 foreach($recent as $r){$html.='<tr><td>#'.(int)$r['id'].'</td><td>'.h(prompt_label($r['prompt_type']??null)).'</td><td>'.h(date_ml($r['received_at'])).'</td><td>'.h(duration($r['duration_ms']===null?null:(int)$r['duration_ms'])).'</td><td>'.h(status_label($r['qc_status'])).'</td><td><a class="btn small outline" href="'.h(linkto('voice',['id'=>$r['id']])).'">Listen & review →</a></td></tr>';}
 if(!$recent)$html.='<tr><td colspan="6">No recordings yet.</td></tr>';
 return $html.'</tbody></table></div><p class="tiny"><a href="'.h(linkto('voices',['search'=>$pid])).'">View all recordings for '.h($pid).' →</a></p></section>';
}
/** Find next not-yet-reviewed eligible recording without bypassing supervisor scoping. */
function next_eligible_review_id(array $u,int $justReviewed): ?int {
 [$scope,$args]=scope_sql($u,'r');
 $row=query("SELECT r.id FROM recordings r
    WHERE r.id<>? AND r.qc_tier3 IS NULL
      AND r.file_status='stored' AND r.is_test=0 AND r.duplicate_of IS NULL
      AND NOT EXISTS (SELECT 1 FROM review_drafts d WHERE d.recording_id=r.id AND d.production_state IN ('pending','failed','confirmed'))
      $scope
    ORDER BY CASE WHEN r.qc_status='needs_review' THEN 0 ELSE 1 END, r.received_at ASC,r.id ASC
    LIMIT 1",array_merge([$justReviewed],$args))->fetchColumn();
 return $row===false?null:(int)$row;
}

function voice_page(array $u,int $id): string {
 if(!allowed_record($id,$u)){http_response_code(404);return page_top('Recording unavailable','This recording is not accessible.');}
 $r=query('SELECT * FROM recordings WHERE id=?',[$id])->fetch();
 $logs=query('SELECT d.*,s.display_name FROM review_drafts d JOIN supervisors s ON s.id=d.reviewer_id WHERE d.recording_id=? ORDER BY d.id DESC LIMIT 50',[$id])->fetchAll();
 $html=page_top('Recording #'.$id,'Listen and record an assigned Tier 3 human quality review.','PRODUCTION · SUPERVISOR REVIEW');
 $html.='<div class="metrics four">'.box_metric('Contributor ID',$r['participant_id'],'Assigned contributor','contributor',['id'=>$r['participant_id']]).box_metric('Prompt type',prompt_label($r['prompt_type']??null),'Verified production metadata').box_metric('Duration',duration($r['duration_ms']===null?null:(int)$r['duration_ms'])).box_metric('Arrival QC',status_label($r['qc_status']),'Tier 1 · bot').'</div>';
 $html.='<div class="metrics three">'.box_metric('Tier 2 ASR',$r['qc_tier2']!==null?(string)$r['qc_tier2'].' / 4':'—',h($r['band_reason']??'Awaiting ASR')).box_metric('Tier 3 supervisor',$r['qc_tier3']===null?'Not reviewed':ucfirst($r['qc_tier3']),h($r['qc_tier3_reason']??'')).box_metric('Final QC',$r['qc_final']!==null?ucfirst($r['qc_final']):'Not yet available','Owner database triggers').'</div>';
 $html.='<section class="panel"><p class="tiny">Received (Malawi): '.h(date_ml($r['received_at'])).' · File status: '.h($r['file_status']).'</p><h2>Listen to the recording</h2><p class="muted">Audio remains private on DigitalOcean and is streamed through the assigned contributor permission checks.</p>';
 if(!sync_ready())$html.='<div class="notice warning">Secure production audio gateway is unavailable.</div>';
 elseif($r['file_status']!=='stored')$html.='<div class="notice warning">This recording is not stored.</div>';
 else $html.='<audio class="audio-player" controls preload="metadata" data-production-audio src="'.h(linkto('audio',['id'=>$id])).'"></audio><p class="tiny" data-audio-message role="status" aria-live="polite">Checking private audio availability…</p><button class="btn outline small" type="button" data-audio-retry>Retry audio</button>';
 $html.='</section><section class="panel"><h2>Tier 3 human review</h2><p class="muted">DigitalOcean stores each confirmed decision as a new append-only audit row. Reversals are recorded separately. Bot Tier 1 remains unchanged.</p>';
 $writeReady=official_write_status()==='ready';
 if(!$writeReady)$html.='<div class="notice warning">Human-review write-back remains disabled pending Dunstan’s migration and deployment approval. Save an online note without changing official QC.</div>';
 if($r['qc_tier3']!==null)$html.='<div class="notice">Current Tier 3: <strong>'.h(ucfirst($r['qc_tier3'])).'</strong>. Changing this decision requires explicit reversal confirmation.</div>';
 $options=$writeReady?['flag'=>'Save online note / flag','accept'=>'Accept recording','reject'=>'Reject recording']:['flag'=>'Save online note / flag'];
 $reasons=[''=>'Select reason','wrong_sentence'=>'Wrong sentence','no_speech'=>'No speech','noise'=>'Background noise','not_own_voice'=>'Not own voice','unclear'=>'Unclear audio','not_chichewa'=>'Not Chichewa','other'=>'Other (explain below)'];
 $html.='<form method="post" action="'.h(linkto('save_review')).'" class="stack">'.csrf_input().'<input type="hidden" name="recording_id" value="'.$id.'"><div class="formgrid"><label>Review decision<select name="decision" data-tier3-decision>'.select_options($options,'flag').'</select></label><label data-rejection-reason hidden>Rejection reason (required for Reject)<select name="reason_code">'.select_options($reasons,'').'</select></label></div><label>Supervisor note<textarea name="notes" maxlength="500" rows="4" placeholder="Reason or follow-up note. Required for Other rejection reason."></textarea></label>';
 if($r['qc_tier3']!==null)$html.='<label class="confirm-row"><input type="checkbox" name="reversal_confirm" value="1"><span><strong>I acknowledge a reversal creates a new permanent audit row.</strong></span></label>';
 $html.='<label class="confirm-row"><input type="checkbox" name="listened" value="1" required><span><strong>I have listened to this recording</strong><small>Reviewer identity, decision and reason are recorded with the time.</small></span></label><div><button class="btn primary" type="submit">Submit review / save note</button></div></form></section>';
 $html.=panel_start('Online review submissions','DigitalOcean is authoritative for confirmed human decisions; unconfirmed drafts never count as QC.');
 $html.='<div class="table-scroll"><table><thead><tr><th>Date (Malawi)</th><th>Reviewer</th><th>Decision</th><th>Production status</th><th>Reason / note</th></tr></thead><tbody>';
 foreach($logs as $log){
  $state=(string)($log['production_state']??'draft');$badge=match($state){'confirmed'=>'Confirmed','pending'=>'Sending','failed'=>'Unconfirmed','conflict'=>'State conflict',default=>'Online note'};
  $html.='<tr><td>'.h(date_ml($log['created_at'])).'</td><td>'.h($log['display_name']).'</td><td>'.h(ucfirst($log['decision'])).'</td><td><span class="pill '.($state==='confirmed'?'accepted':($state==='failed'||$state==='conflict'?'rejected':'pending')).'">'.h($badge).'</span></td><td>'.h(trim((string)($log['reason_code']??'').' · '.(string)$log['notes']));
  if(($state==='failed'||$state==='pending') && ($u['role']==='admin'||(int)$log['reviewer_id']===(int)$u['id']))$html.='<form method="post" action="'.h(linkto('retry_review')).'" class="retry-review">'.csrf_input().'<input type="hidden" name="review_id" value="'.(int)$log['id'].'"><button class="btn small outline" type="submit">Retry same operation</button></form>';
  $html.='</td></tr>';
 }
 if(!$logs)$html.='<tr><td colspan="5" class="no-data">No reviews have been submitted for this recording.</td></tr>';
 return $html.'</tbody></table></div></section>';
}

function followups_page(array $u): string {
 [$scope,$args]=scope_sql($u);
 $attention=query("SELECT r.id,r.participant_id,r.received_at,r.qc_status FROM recordings r WHERE r.qc_status='needs_review' $scope ORDER BY r.received_at DESC LIMIT 100",$args)->fetchAll();
 $q="SELECT f.*,s.display_name,r.participant_id FROM followups f JOIN supervisors s ON s.id=f.created_by JOIN recordings r ON r.id=f.recording_id WHERE 1=1 $scope ORDER BY f.id DESC LIMIT 100";
 $followups=query($q,$args)->fetchAll();
 $html=page_top('Escalation follow-ups','Review flagged production recordings and confirmed supervisor escalations.');
 $html.='<div class="metrics two">'.box_metric('Needs human review',(string)count($attention),'Production arrival-QC flags','queue',['qc'=>'needs_review']).box_metric('Saved online follow-up actions',(string)count($followups),'Confirmed escalations and online follow-ups','audit').'</div>';
 $html.=panel_start('Arrival QC attention','Flagged by the production collector. These do not automatically become escalations.');
 $html.='<div class="table-scroll"><table><thead><tr><th>Recording</th><th>Contributor</th><th>Received</th><th>QC</th><th></th></tr></thead><tbody>';
 foreach($attention as $r)$html.='<tr><td>#'.h($r['id']).'</td><td>'.h($r['participant_id']).'</td><td>'.h(date_ml($r['received_at'])).'</td><td>'.h(status_label($r['qc_status'])).'</td><td><a class="btn small outline" href="'.h(linkto('voice',['id'=>$r['id']])).'">Listen & review →</a></td></tr>';
 if(!$attention)$html.='<tr><td colspan="5" class="no-data">No visible recordings currently flagged for human review.</td></tr>';
 $html.='</tbody></table></div></section>';
 $html.=panel_start('Manual online follow-up history','Audited online follow-ups. WhatsApp delivery remains a separate approved workflow.');
 $html.='<div class="table-scroll"><table><thead><tr><th>Recording</th><th>Contributor</th><th>State</th><th>Reviewer</th><th>Notes</th><th>Date</th></tr></thead><tbody>';
 foreach($followups as $f)$html.='<tr><td><a href="'.h(linkto('voice',['id'=>$f['recording_id']])).'">#'.h($f['recording_id']).'</a></td><td>'.h($f['participant_id']).'</td><td>'.h(ucfirst($f['state'])).'</td><td>'.h($f['display_name']).'</td><td>'.h($f['notes']).'</td><td>'.h(date_ml($f['created_at'])).'</td></tr>';
 if(!$followups)$html.='<tr><td colspan="6" class="no-data">No manual follow-up records yet.</td></tr>';
 return $html.'</tbody></table></div></section>';
}
function report_data(array $u,string $day): array {
 [$start,$end]=ml_day_bounds($day);[$scope,$sargs]=scope_sql($u);
 $params=array_merge([$start,$end],$sargs);
 $sql="SELECT r.participant_id,COUNT(*) AS total,SUM(r.qc_status='pending') AS pending,SUM(r.qc_status='needs_review') AS flagged,SUM(r.qc_status='rejected') AS rejected,SUM(r.qc_status='accepted') AS accepted,ROUND(SUM(COALESCE(r.duration_ms,0))/3600000,4) AS recorded_hours FROM recordings r WHERE r.received_at>=? AND r.received_at<? $scope GROUP BY r.participant_id ORDER BY total DESC,r.participant_id";
 return query($sql,$params)->fetchAll();
}
function report_page(array $u): string {
 $day=get_str('day',ml_today());try{$rows=report_data($u,$day);}catch(InvalidArgumentException){$day=ml_today();$rows=report_data($u,$day);}
 $sum=['total'=>0,'pending'=>0,'flagged'=>0,'rejected'=>0,'accepted'=>0];foreach($rows as $r){foreach($sum as $key=>$v)$sum[$key]+=(int)$r[$key];}
 $html=page_top('Daily supervision report','Production arrival-QC activity grouped by contributor, selected by Malawi local day. Official QC is retrieved from DigitalOcean following confirmed decisions.');
 $html.='<div class="panel filters"><form method="get" class="filter-grid"><input type="hidden" name="page" value="reports"><label>Report date (Malawi)<input type="date" name="day" value="'.h($day).'"></label><div class="form-buttons"><button class="btn primary" type="submit">Load report</button><a class="btn outline" href="'.h(linkto('export',['day'=>$day])).'">Export CSV</a></div></form></div>';
 $html.='<div class="metrics four">'.box_metric('Production recordings',(string)$sum['total'],'Received on '.$day,'recordings',['day'=>$day]).box_metric('QC pending',(string)$sum['pending'],'Awaiting checks','queue',['day'=>$day,'qc'=>'pending']).box_metric('Needs human review',(string)$sum['flagged'],'Flagged on arrival','queue',['day'=>$day,'qc'=>'needs_review']).box_metric('QC rejected',(string)$sum['rejected'],'Rejected on arrival','voices',['day'=>$day,'qc'=>'rejected']).'</div>';
 $html.=panel_start('Contributor quality','Production statuses reflect the latest successful synchronization; unconfirmed reviews do not change official QC.');
 $html.='<div class="table-scroll"><table><thead><tr><th>Contributor</th><th>Recorded</th><th>QC accepted</th><th>Pending</th><th>Needs review</th><th>Rejected</th><th>Recorded duration</th></tr></thead><tbody>';
 foreach($rows as $r)$html.='<tr><td><a href="'.h(linkto('voices',['search'=>$r['participant_id'],'day'=>$day])).'">'.h($r['participant_id']).'</a></td><td>'.h($r['total']).'</td><td>'.h($r['accepted']).'</td><td>'.h($r['pending']).'</td><td>'.h($r['flagged']).'</td><td>'.h($r['rejected']).'</td><td>'.h((string)$r['recorded_hours']).' h (raw)</td></tr>';
 if(!$rows)$html.='<tr><td colspan="7" class="no-data">No synchronized production recordings on this day. Check your date and last sync.</td></tr>';
 return $html.'</tbody></table></div></section>';
}
function export_csv(array $u): never {
 $day=get_str('day',ml_today());$rows=report_data($u,$day);audit('report_export','day',$day,'Daily production CSV');
 header('Content-Type: text/csv; charset=utf-8');header('Content-Disposition: attachment; filename="supervision-report-'.$day.'.csv"');header('Cache-Control: private,no-store');
 $fp=fopen('php://output','w');fputcsv($fp,['Contributor','Recordings','QC Accepted','Pending','Needs Review','Rejected','Recorded Hours (raw)']);
 foreach($rows as $r){$vals=[$r['participant_id'],$r['total'],$r['accepted'],$r['pending'],$r['flagged'],$r['rejected'],$r['recorded_hours']];foreach($vals as &$v)if(is_string($v)&&preg_match('/^[\s]*[=+@-]/',$v))$v="'".$v;unset($v);fputcsv($fp,$vals);}fclose($fp);exit;
}
