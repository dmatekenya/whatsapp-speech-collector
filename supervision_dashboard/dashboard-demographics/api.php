<?php
/**
 * Masakhane admin Overview — public-safe aggregate proxy.
 * This endpoint contains NO credentials, performs NO database writes and only
 * retrieves aggregated statistics already published by ZaZoAI.
 */
declare(strict_types=1);
// Require a signed-in admin. Project-wide aggregates are not scoped by supervisor.
require_once dirname(__DIR__) . '/app/bootstrap.php';
if (!viewer()) {
    http_response_code(401);
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store');
    echo '{"status":"authentication_required"}';
    exit;
}
$demographicUser = viewer();
if (($demographicUser['role'] ?? '') !== 'admin') {
    http_response_code(403);
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store');
    echo '{"status":"access_denied"}';
    exit;
}
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store, private, max-age=0, must-revalidate');
header('Pragma: no-cache');
header('Expires: 0');
header('X-Content-Type-Options: nosniff');
header('X-Robots-Tag: noindex, nofollow');
if (($_SERVER['REQUEST_METHOD'] ?? 'GET') !== 'GET') {
    http_response_code(405);
    header('Allow: GET');
    echo '{"status":"method_not_allowed"}';
    exit;
}

// Only this fixed upstream URL is contacted: no caller-controlled URLs.
$upstream = 'https://zazoai.com/api/speech-progress.php?dashboard_demographics=' . rawurlencode((string)time());
$raw = false;
$code = 0;
if (function_exists('curl_init')) {
    $handle = curl_init($upstream);
    curl_setopt_array($handle, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_CONNECTTIMEOUT => 3,
        CURLOPT_TIMEOUT => 6,
        CURLOPT_FOLLOWLOCATION => false,
        CURLOPT_SSL_VERIFYPEER => true,
        CURLOPT_SSL_VERIFYHOST => 2,
        CURLOPT_HTTPHEADER => ['Accept: application/json', 'Cache-Control: no-cache'],
        CURLOPT_USERAGENT => 'MasakhaneDashboardDemographics/1.0',
    ]);
    $raw = curl_exec($handle);
    $code = (int)curl_getinfo($handle, CURLINFO_HTTP_CODE);
    curl_close($handle);
} else {
    $context = stream_context_create([
        'http' => ['method' => 'GET', 'timeout' => 6, 'ignore_errors' => true,
                   'header' => "Accept: application/json\r\nCache-Control: no-cache\r\n"],
        'ssl' => ['verify_peer' => true, 'verify_peer_name' => true],
    ]);
    $raw = @file_get_contents($upstream, false, $context);
    $code = ($raw !== false) ? 200 : 503;
}
if (!is_string($raw) || $code < 200 || $code >= 300 || strlen($raw) > 250000) {
    http_response_code(503);
    echo '{"status":"unavailable","message":"Published demographic figures are temporarily unavailable."}';
    exit;
}
try {
    $data = json_decode($raw, true, 32, JSON_THROW_ON_ERROR);
    if (!is_array($data) || ($data['version'] ?? null) !== 1
        || ($data['demographics_status'] ?? null) !== 'live'
        || !is_array($data['speaker_dimensions'] ?? null)) {
        throw new RuntimeException('No compatible verified public snapshot');
    }
    $dimensions = $data['speaker_dimensions'];
    $required = ['gender', 'age_group', 'first_language', 'district'];
    $safe = [];
    $totalMs = (int)($data['demographics_total_ms'] ?? $data['recorded_ms'] ?? 0);
    if ($totalMs <= 0) {
        throw new RuntimeException('Missing duration');
    }
    foreach ($required as $field) {
        if (!is_array($dimensions[$field] ?? null)) {
            throw new RuntimeException('Missing dimension');
        }
        $list = [];
        $sum = 0;
        foreach ($dimensions[$field] as $row) {
            if (!is_array($row)) throw new RuntimeException('Malformed row');
            $name = $row['name'] ?? null;
            $ms = $row['duration_ms'] ?? null;
            if (!is_string($name) || strlen($name) > 130 || !is_int($ms) || $ms < 0) {
                throw new RuntimeException('Malformed aggregate');
            }
            $sum += $ms;
            $list[] = ['name' => $name, 'duration_ms' => $ms,
                'contributors' => is_int($row['contributors'] ?? null) && $row['contributors'] >= 0
                    ? $row['contributors'] : null];
        }
        if (abs($sum - $totalMs) > max(120000, (int)round($totalMs * 0.03))) {
            throw new RuntimeException('Aggregate totals inconsistent');
        }
        $safe[$field] = $list;
    }

    // If the publisher adds a verified country dimension in the future, prefer it.
    $countrySource = 'not_recorded';
    if (is_array($dimensions['country'] ?? null)) {
        $country = [];
        $countrySum = 0;
        foreach ($dimensions['country'] as $row) {
            if (!is_array($row) || !is_string($row['name'] ?? null)
                || !is_int($row['duration_ms'] ?? null) || $row['duration_ms'] < 0) {
                throw new RuntimeException('Invalid country breakdown');
            }
            $country[] = [
                'name' => substr($row['name'], 0, 130),
                'duration_ms' => $row['duration_ms'],
                'contributors' => is_int($row['contributors'] ?? null) ? $row['contributors'] : null,
            ];
            $countrySum += $row['duration_ms'];
        }
        if (abs($countrySum - $totalMs) <= max(120000, (int)round($totalMs * 0.03))) {
            $safe['country'] = $country;
            $countrySource = 'recorded';
        }
    }
    if ($countrySource !== 'recorded') {
        // Country was NOT collected in the supplied PostgreSQL schema.
        // Infer Malawi ONLY for recognizable Malawi district names. Small
        // suppressed groups must remain unverified; never allocate them blindly.
        $malawiDistricts = [
            'balaka','blantyre','chikwawa','chiradzulu','chitipa','dedza',
            'dowa','karonga','kasungu','likoma','lilongwe','machinga',
            'mangochi','mchinji','mulanje','mwanza','mzimba','mzuzu',
            'neno','nkhata bay','nkhatabay','nkhotakota','nsanje','ntcheu',
            'ntchisi','phalombe','rumphi','salima','thyolo','zomba'
        ];
        $known = 0;
        foreach ($safe['district'] as $row) {
            if (in_array(strtolower(trim($row['name'])), $malawiDistricts, true)) {
                $known += $row['duration_ms'];
            }
        }
        $unknown = max(0, $totalMs - $known);
        $safe['country'] = [];
        if ($known > 0) $safe['country'][] = ['name' => 'Malawi (district-derived)', 'duration_ms' => $known, 'contributors' => null];
        if ($unknown > 0) $safe['country'][] = ['name' => 'Country unverified', 'duration_ms' => $unknown, 'contributors' => null];
        $countrySource = 'derived_from_district';
    }
    $response = [
        'status' => 'live',
        'total_ms' => $totalMs,
        'generated_at' => (string)($data['demographics_updated_at'] ?? ''),
        'country_source' => $countrySource,
        'dimensions' => [
            'gender' => $safe['gender'],
            'country' => $safe['country'],
            'age_group' => $safe['age_group'],
            'first_language' => $safe['first_language'],
        ],
    ];
    echo json_encode($response, JSON_UNESCAPED_UNICODE | JSON_INVALID_UTF8_SUBSTITUTE | JSON_THROW_ON_ERROR);
} catch (Throwable $error) {
    error_log('Masakhane demographics proxy: ' . get_class($error));
    http_response_code(503);
    echo '{"status":"unavailable","message":"Verified demographic aggregates are not yet available."}';
}
