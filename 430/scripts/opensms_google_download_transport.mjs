// Retry only a fresh read from the configured Google export URL. No old file,
// alternate account, browser focus, or write operation is used as a fallback.
export async function downloadGoogleWorkbook(url, {
  // A slow but progressing XLSX must not be restarted every 20 seconds.
  // Fast transient failures may retry, but all attempts share one 60s deadline.
  fetchImpl=fetch, totalTimeoutMs=60000, attemptTimeoutMs=60000, maxAttempts=3,
  now=()=>performance.now(), sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms)),
}={}) {
  const started=now(), attempts=[];
  const transportCodes=new Set(['ECONNRESET','ECONNREFUSED','ETIMEDOUT','EAI_AGAIN','ENOTFOUND',
    'ENETUNREACH','EHOSTUNREACH','UND_ERR_CONNECT_TIMEOUT','UND_ERR_HEADERS_TIMEOUT',
    'UND_ERR_BODY_TIMEOUT','UND_ERR_SOCKET']);
  const fail=reason=>Object.assign(new Error(`Google Sheet 新鲜下载失败：${reason}`),
    {code:'OPENSMS_GOOGLE_DOWNLOAD_FAILED',downloadAttempts:attempts,
      googleDownloadDiagnostic: {
        category: ['TimeoutError','AbortError','ETIMEDOUT','UND_ERR_CONNECT_TIMEOUT',
          'UND_ERR_HEADERS_TIMEOUT','UND_ERR_BODY_TIMEOUT'].includes(attempts.at(-1)?.status)
          ? 'read_timeout' : attempts.at(-1)?.status === 'INVALID_XLSX' ? 'invalid_xlsx'
            : /^HTTP_\d{3}$/.test(attempts.at(-1)?.status || '') ? 'http_error' : 'transport_error',
        attemptCount: attempts.length, elapsedMs: Math.round(now()-started),
      }});
  if(!(Number.isInteger(maxAttempts)&&maxAttempts>=1&&maxAttempts<=3
    &&Number.isFinite(totalTimeoutMs)&&totalTimeoutMs>0&&totalTimeoutMs<=60000
    &&Number.isFinite(attemptTimeoutMs)&&attemptTimeoutMs>0)) {
    throw new Error('Google 下载重试参数无效');
  }
  for(let attempt=1;attempt<=maxAttempts;attempt++) {
    const remaining=totalTimeoutMs-(now()-started);
    if(remaining<=0) throw fail('总等待时间已达上限');
    const at=now();
    const timeoutMs=Math.max(1,Math.floor(Math.min(attemptTimeoutMs,remaining)));
    let response, bytes, phase='request', receivedBytes=0;
    try {
      response=await fetchImpl(url,{method:'GET',redirect:'follow',
        signal:AbortSignal.timeout(timeoutMs)});
      if(response.ok) {
        phase='response_body';
        if(response.body?.getReader) {
          const reader=response.body.getReader(), chunks=[];
          try {
            for(;;) {
              const {done,value}=await reader.read();
              if(done) break;
              const chunk=Buffer.from(value);
              receivedBytes+=chunk.length;chunks.push(chunk);
            }
          } finally {reader.releaseLock();}
          bytes=Buffer.concat(chunks,receivedBytes);
        } else {
          bytes=Buffer.from(await response.arrayBuffer());receivedBytes=bytes.length;
        }
      }
    } catch(error) {
      const code=transportCodes.has(error?.cause?.code) ? error.cause.code
        : ['AbortError','TimeoutError'].includes(error?.name) ? error.name : 'UNCLASSIFIED_TRANSPORT_ERROR';
      attempts.push({attempt,status:code,phase,httpStatus:response?.status||null,
        receivedBytes,timeoutMs,elapsedMs:Math.round(now()-at)});
      if(code==='UNCLASSIFIED_TRANSPORT_ERROR'||attempt===maxAttempts) throw fail(code);
      const delay=500*attempt;
      if(now()-started+delay>=totalTimeoutMs) throw fail('总等待时间已达上限');
      await sleep(delay);continue;
    }
    if(!response.ok) {
      attempts.push({attempt,status:`HTTP_${response.status}`,timeoutMs,elapsedMs:Math.round(now()-at)});
      await response.body?.cancel().catch(()=>{});
      const retryable=response.status===429 || [500,502,503,504].includes(response.status);
      if(!retryable||attempt===maxAttempts) throw fail(`HTTP ${response.status}`);
      const retryAfter=response.headers.get('retry-after');
      let retryDelay=500*attempt;
      if(retryAfter) {
        const seconds=Number(retryAfter);
        const delay=Number.isFinite(seconds)?seconds*1000:Date.parse(retryAfter)-Date.now();
        if(Number.isFinite(delay)) retryDelay=Math.max(retryDelay,delay);
      }
      if(now()-started+retryDelay>=totalTimeoutMs) throw fail(`HTTP ${response.status}，重试等待超过总时限`);
      await sleep(retryDelay);continue;
    }
    if(bytes.length<10000||bytes[0]!==0x50||bytes[1]!==0x4b) {
      attempts.push({attempt,status:'INVALID_XLSX',size:bytes.length,timeoutMs,elapsedMs:Math.round(now()-at)});
      throw fail('下载内容不是有效 XLSX，未保存、未复用旧文件');
    }
    attempts.push({attempt,status:'downloaded',size:bytes.length,timeoutMs,elapsedMs:Math.round(now()-at)});
    return {bytes,contentType:response.headers.get('content-type')||'',attempts,elapsedMs:Math.round(now()-started)};
  }
  throw fail('重试次数已达上限');
}
