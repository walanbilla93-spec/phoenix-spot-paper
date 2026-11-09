"""Process and journal measurements; includes all worker threads' CPU time."""
import os,time
from pathlib import Path

class Metrics:
    def __init__(self):
        self.wall=time.monotonic();self.cpu=time.process_time();self.samples=[];self.cycles=[]
        self.origin=self.wall;self.origin_cpu=self.cpu;self.fraction=0
    def sample(self, storage, allocated=None):
        now=time.monotonic();cpu=time.process_time();wall=max(now-self.wall,.001)
        if wall>=5:
            self.fraction=(cpu-self.cpu)/wall;self.wall=now;self.cpu=cpu
        fraction=self.fraction
        rss=peak=None
        if os.name=='posix':
            fields={line.split(':')[0]:line.split(':')[1].strip() for line in Path('/proc/self/status').read_text().splitlines() if ':' in line}
            rss=int(fields['VmRSS'].split()[0])*1024;peak=int(fields['VmHWM'].split()[0])*1024
        elif os.name=='nt':
            import ctypes
            from ctypes import wintypes
            class Counters(ctypes.Structure):
                _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD)]+[(x,ctypes.c_size_t) for x in
                    ('PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage')]
            c=Counters();c.cb=ctypes.sizeof(c)
            fn=ctypes.windll.psapi.GetProcessMemoryInfo
            fn.argtypes=[wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD];fn.restype=wintypes.BOOL
            if fn(wintypes.HANDLE(-1),ctypes.byref(c),c.cb):rss=c.WorkingSetSize;peak=c.PeakWorkingSetSize
        if not self.samples or now-self.samples[-1][0]>=5:self.samples.append((now,allocated if allocated is not None else storage))
        self.samples=self.samples[-60:]
        elapsed=now-self.samples[0][0]
        growth=((allocated if allocated is not None else storage)-self.samples[0][1])/elapsed*3600 if elapsed>=60 else None
        return dict(rss_bytes=rss,peak_rss_bytes=peak,cpu_vcpu=fraction,
            cpu_percent_of_free_0_1_vcpu=fraction/0.1*100,storage_bytes=storage,
            lifetime_cpu_vcpu=(cpu-self.origin_cpu)/max(now-self.origin,.001),allocated_database_bytes=allocated,
            storage_growth_bytes_per_hour=growth,storage_measurement_seconds=elapsed,
            last_cycle_seconds=self.cycles[-1] if self.cycles else None,
            max_cycle_seconds=max(self.cycles) if self.cycles else None)
