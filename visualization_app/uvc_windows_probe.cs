// Windows SDK vidcap.h/ksproxy.h 的 XU 查询及 DirectShow 原始样本诊断；不设置测量参数。
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Web.Script.Serialization;
using System.Threading;
using System.IO;
using System.Diagnostics;
using System.Text;

[ComImport, Guid("29840822-5B84-11D0-BD3B-00A0C911CE86"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface ICreateDevEnum {
    [PreserveSig] int CreateClassEnumerator(ref Guid category, out IEnumMoniker enumerator, int flags);
}
[ComImport, Guid("55272A00-42CB-11CE-8135-00AA004BB851"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IPropertyBag {
    [PreserveSig] int Read([MarshalAs(UnmanagedType.LPWStr)] string name, [MarshalAs(UnmanagedType.Struct)] out object value, IntPtr errorLog);
    [PreserveSig] int Write([MarshalAs(UnmanagedType.LPWStr)] string name, ref object value);
}
[ComImport, Guid("56A868A9-0AD4-11CE-B03A-0020AF0BA770"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IGraphBuilder {
    [PreserveSig] int AddFilter(IBaseFilter filter, [MarshalAs(UnmanagedType.LPWStr)] string name);
    [PreserveSig] int RemoveFilter(IBaseFilter filter);
    [PreserveSig] int EnumFilters(out IntPtr filters);
    [PreserveSig] int FindFilterByName([MarshalAs(UnmanagedType.LPWStr)]string name,out IBaseFilter filter);
    [PreserveSig] int ConnectDirect(IPin output,IPin input,IntPtr mediaType);
}
[ComImport, Guid("56A86895-0AD4-11CE-B03A-0020AF0BA770"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IBaseFilter {
    [PreserveSig] int GetClassID(out Guid clsid);
    [PreserveSig] int Stop(); [PreserveSig] int Pause(); [PreserveSig] int Run(long start);
    [PreserveSig] int GetState(uint timeout,out int state);
    [PreserveSig] int SetSyncSource(IntPtr clock); [PreserveSig] int GetSyncSource(out IntPtr clock);
    [PreserveSig] int EnumPins(out IEnumPins pins);
}
[ComImport,Guid("56A86892-0AD4-11CE-B03A-0020AF0BA770"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IEnumPins {
    [PreserveSig] int Next(uint count,out IPin pin,IntPtr fetched);
    [PreserveSig] int Skip(uint count);[PreserveSig] int Reset();[PreserveSig] int Clone(out IEnumPins pins);
}
[ComImport,Guid("56A86891-0AD4-11CE-B03A-0020AF0BA770"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IPin {
    [PreserveSig] int Connect(IPin pin,IntPtr mediaType);
    [PreserveSig] int ReceiveConnection(IPin pin,IntPtr mediaType);
    [PreserveSig] int Disconnect();[PreserveSig] int ConnectedTo(out IPin pin);
    [PreserveSig] int ConnectionMediaType(IntPtr mediaType);
    [PreserveSig] int QueryPinInfo(IntPtr info);
    [PreserveSig] int QueryDirection(out int direction);
}
[ComImport,Guid("C6E13340-30AC-11D0-A18C-00A0C9118956"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAMStreamConfig {
    [PreserveSig] int SetFormat(IntPtr mediaType);
    [PreserveSig] int GetFormat(out IntPtr mediaType);
    [PreserveSig] int GetNumberOfCapabilities(out int count,out int size);
    [PreserveSig] int GetStreamCaps(int index,out IntPtr mediaType,IntPtr caps);
}
[StructLayout(LayoutKind.Sequential)]
struct AMMediaType {
    public Guid Major,Sub;public int FixedSize,TemporalCompression;public uint SampleSize;
    public Guid Format;public IntPtr Unknown;public uint FormatSize;public IntPtr FormatData;
}
[ComImport,Guid("6B652FFF-11FE-4FCE-92AD-0266B5D7C78F"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface ISampleGrabber {
    [PreserveSig] int SetOneShot([MarshalAs(UnmanagedType.Bool)]bool oneShot);
    [PreserveSig] int SetMediaType(ref AMMediaType mediaType);
    [PreserveSig] int GetConnectedMediaType(out AMMediaType mediaType);
    [PreserveSig] int SetBufferSamples([MarshalAs(UnmanagedType.Bool)]bool buffer);
    [PreserveSig] int GetCurrentBuffer(ref int size,IntPtr buffer);
    [PreserveSig] int GetCurrentSample(out IntPtr sample);
    [PreserveSig] int SetCallback(ISampleGrabberCB callback,int method);
}
[ComVisible(true),Guid("0579154A-2B53-4994-B0D0-E773148EFF85"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface ISampleGrabberCB {
    [PreserveSig] int SampleCB(double time,IntPtr sample);
    [PreserveSig] int BufferCB(double time,IntPtr buffer,int size);
}
[ComImport,Guid("56A868B1-0AD4-11CE-B03A-0020AF0BA770"),InterfaceType(ComInterfaceType.InterfaceIsDual)]
interface IMediaControl {
    [PreserveSig] int Run();[PreserveSig] int Pause();[PreserveSig] int Stop();
    [PreserveSig] int GetState(int timeout,out int state);
}
[ComVisible(true),ClassInterface(ClassInterfaceType.None)]
public class RawCallback : ISampleGrabberCB {
    public readonly BinaryWriter Writer=new BinaryWriter(Console.OpenStandardOutput());
    public int Frames=0; public string Error="";
    readonly Stopwatch Clock=Stopwatch.StartNew();
    public int SampleCB(double time,IntPtr sample) { return 0; }
    public int BufferCB(double time,IntPtr buffer,int size) {
        try {
            if(size<=0 || size>16*1024*1024) throw new InvalidDataException("Invalid raw sample size");
            byte[] data=new byte[size];Marshal.Copy(buffer,data,0,size);
            lock(Writer) {
                Writer.Write(size);Writer.Write(data);Writer.Flush();Frames++;
                Console.Error.WriteLine(new JavaScriptSerializer().Serialize(new {
                    record_type="raw_sample",transport_ordinal=Frames,payload_length=size,
                    directshow_sample_time_seconds=time,host_elapsed_seconds=Clock.Elapsed.TotalSeconds,
                    source_sequence_confirmed=false,temperature_ready=false }));
            }
            return 0;
        } catch(Exception ex) { Error=ex.Message;return ex.HResult; }
    }
}
[ComImport, Guid("720D4AC0-7533-11D0-A5D6-28DB04C10000"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IKsTopologyInfo {
    [PreserveSig] int NumCategories(out uint count);
    [PreserveSig] int Category(uint index, out Guid category);
    [PreserveSig] int NumConnections(out uint count);
    [PreserveSig] int Connection(uint index, IntPtr connection);
    [PreserveSig] int NodeName(uint node, IntPtr name, uint size, out uint length);
    [PreserveSig] int NumNodes(out uint count);
    [PreserveSig] int NodeType(uint node, out Guid type);
    [PreserveSig] int CreateNodeInstance(uint node, ref Guid iid, [MarshalAs(UnmanagedType.IUnknown)] out object instance);
}
[ComImport, Guid("28F54685-06FD-11D2-B27A-00A0C9223196"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IKsControl {
    [PreserveSig] int KsProperty(ref KsNode property, uint propertySize, IntPtr data, uint dataSize, out uint returned);
    [PreserveSig] int KsMethod(IntPtr method, uint methodSize, IntPtr data, uint dataSize, out uint returned);
    [PreserveSig] int KsEvent(IntPtr ev, uint evSize, IntPtr data, uint dataSize, out uint returned);
}
[StructLayout(LayoutKind.Sequential)]
struct KsNode { public Guid Set; public uint Id; public uint Flags; public uint NodeId; public uint Reserved; }

class UvcWindowsProbe {
    static string Hr(int value) { return "0x" + unchecked((uint)value).ToString("X8"); }
    static void Check(int value) { if(value < 0) Marshal.ThrowExceptionForHR(value); }
    static void Release(object obj) { if(obj != null && Marshal.IsComObject(obj)) Marshal.ReleaseComObject(obj); }
    static void FreeMedia(IntPtr ptr) {
        if(ptr==IntPtr.Zero)return;
        AMMediaType mt=(AMMediaType)Marshal.PtrToStructure(ptr,typeof(AMMediaType));
        if(mt.FormatData!=IntPtr.Zero)Marshal.FreeCoTaskMem(mt.FormatData);
        if(mt.Unknown!=IntPtr.Zero)Marshal.Release(mt.Unknown);
        Marshal.FreeCoTaskMem(ptr);
    }
    static Dictionary<string,object> MediaInfo(AMMediaType mt) {
        var info=new Dictionary<string,object> {
            {"major_type",mt.Major.ToString()},{"subtype",mt.Sub.ToString()},
            {"format_type",mt.Format.ToString()},{"sample_size",mt.SampleSize},
            {"fixed_size_samples",mt.FixedSize!=0},{"format_size",mt.FormatSize} };
        if(mt.Format==new Guid("05589F80-C356-11CE-BF01-00AA0055595A")&&mt.FormatSize>=88&&mt.FormatData!=IntPtr.Zero) {
            info["width"]=Marshal.ReadInt32(mt.FormatData,52);
            info["height"]=Marshal.ReadInt32(mt.FormatData,56);
            info["image_size"]=Marshal.ReadInt32(mt.FormatData,68);
            info["average_frame_interval_100ns"]=Marshal.ReadInt64(mt.FormatData,40);
        }
        return info;
    }
    static Dictionary<string,object> StreamParameter(object filter,Guid xu) {
        var topology=(IKsTopologyInfo)filter;uint count;Check(topology.NumNodes(out count));
        Dictionary<string,object> value=null;
        for(uint i=0;i<count;i++) {
            object node=null;
            try {
                Guid iid=typeof(IKsControl).GUID;
                if(topology.CreateNodeInstance(i,ref iid,out node)<0)continue;
                var control=(IKsControl)node;
                if((string)Get(control,i,xu,4,4)["data_hex"]!="322E3000")continue;
                if(value!=null)throw new InvalidDataException("Ambiguous protocol 2.0 extension nodes");
                value=SelectRead(control,i,xu,3,5,1600);
            } finally {Release(node);}
        }
        if(value==null)throw new InvalidDataException("Protocol 2.0 extension node missing");
        return value;
    }
    static IPin Pin(IBaseFilter filter,int direction) {
        IEnumPins pins;Check(filter.EnumPins(out pins));
        try { IPin pin;while(pins.Next(1,out pin,IntPtr.Zero)==0) {
            int d;Check(pin.QueryDirection(out d));if(d==direction)return pin;Release(pin);
        }} finally {Release(pins);}
        throw new InvalidOperationException("Required pin missing");
    }
    static void Stream(object graph,object filter,Dictionary<string,object> result,int seconds,int hostHeight,Guid xu) {
        object grabber=null,sink=null;IPin source=null,input=null,output=null,sinkPin=null;
        try {
            source=Pin((IBaseFilter)filter,1);var config=(IAMStreamConfig)source;
            int count,size;Check(config.GetNumberOfCapabilities(out count,out size));
            if(size<=0||size>65536||count<=0||count>1024)throw new InvalidDataException("Invalid capability dimensions");
            IntPtr caps=Marshal.AllocCoTaskMem(size), chosen=IntPtr.Zero;
            var formats=new List<object>();result["formats"]=formats;
            try {
                for(int i=0;i<count;i++) {
                    IntPtr ptr;Check(config.GetStreamCaps(i,out ptr,caps));
                    AMMediaType mt=(AMMediaType)Marshal.PtrToStructure(ptr,typeof(AMMediaType));
                    if(mt.Format==new Guid("05589F80-C356-11CE-BF01-00AA0055595A")&&mt.FormatSize>=88) {
                        int w=Marshal.ReadInt32(mt.FormatData,52),h=Marshal.ReadInt32(mt.FormatData,56);
                        int imageSize=Marshal.ReadInt32(mt.FormatData,68);
                        formats.Add(new {width=w,height=h,sample_size=mt.SampleSize,image_size=imageSize,subtype=mt.Sub.ToString()});
                        if(mt.Sub==new Guid("32595559-0000-0010-8000-00AA00389B71")&&w==256&&h==hostHeight&&chosen==IntPtr.Zero) {chosen=ptr;continue;}
                    }
                    FreeMedia(ptr);
                }
                if(chosen==IntPtr.Zero)throw new InvalidDataException("Full raw temperature/YUV capture format unavailable");
                var requested=(AMMediaType)Marshal.PtrToStructure(chosen,typeof(AMMediaType));
                result["advertised_format"]=MediaInfo(requested);
                result["requested_format"]=MediaInfo(requested);
                Check(config.SetFormat(chosen));
                IntPtr actual;Check(config.GetFormat(out actual));
                try {result["configured_format"]=MediaInfo((AMMediaType)Marshal.PtrToStructure(actual,typeof(AMMediaType)));}
                finally {FreeMedia(actual);}
                result["stream_parameter_after_set_format"]=StreamParameter(filter,xu);
            } finally {FreeMedia(chosen);Marshal.FreeCoTaskMem(caps);}
            grabber=Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("C1F400A0-3F08-11D3-9F0B-006008039E37")));
            sink=Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("C1F400A4-3F08-11D3-9F0B-006008039E37")));
            var builder=(IGraphBuilder)graph;Check(builder.AddFilter((IBaseFilter)grabber,"Raw sample grabber"));
            Check(builder.AddFilter((IBaseFilter)sink,"Null sink"));
            input=Pin((IBaseFilter)grabber,0);output=Pin((IBaseFilter)grabber,1);sinkPin=Pin((IBaseFilter)sink,0);
            Check(builder.ConnectDirect(source,input,IntPtr.Zero));Check(builder.ConnectDirect(output,sinkPin,IntPtr.Zero));
            AMMediaType connected;Check(((ISampleGrabber)grabber).GetConnectedMediaType(out connected));
            try {result["connected_format"]=MediaInfo(connected);}
            finally {
                if(connected.FormatData!=IntPtr.Zero)Marshal.FreeCoTaskMem(connected.FormatData);
                if(connected.Unknown!=IntPtr.Zero)Marshal.Release(connected.Unknown);
            }
            var callback=new RawCallback();Check(((ISampleGrabber)grabber).SetCallback(callback,1));
            Console.Error.WriteLine(new JavaScriptSerializer().Serialize(result));
            Check(((ISampleGrabber)grabber).SetBufferSamples(true));
            int runResult=((IMediaControl)graph).Run();Check(runResult);result["run_hresult"]=Hr(runResult);
            result["stream_parameter_after_run"]=StreamParameter(filter,xu);
            var clock=Stopwatch.StartNew();
            while(clock.Elapsed.TotalSeconds<seconds&&callback.Error=="")Thread.Sleep(10);
            int bufferSize=0;result["buffer_hresult"]=Hr(((ISampleGrabber)grabber).GetCurrentBuffer(ref bufferSize,IntPtr.Zero));
            result["buffer_size"]=bufferSize;
            int state;result["graph_state_hresult"]=Hr(((IMediaControl)graph).GetState(1000,out state));result["graph_state"]=state;
            Check(((IMediaControl)graph).Stop());
            result["stream_parameter_after_stop"]=StreamParameter(filter,xu);
            GC.KeepAlive(callback);
            result["raw_samples"]=callback.Frames;
            result["temperature_ready"]=false;
            if(callback.Error!="")throw new IOException(callback.Error);
        } finally {
            try{((IMediaControl)graph).Stop();}catch{}
            Release(sinkPin);Release(output);Release(input);Release(source);Release(sink);Release(grabber);
        }
    }
    static Dictionary<string,object> SelectRead(IKsControl control,uint node,Guid xu,byte main,byte sub,uint size) {
        // 选择查询子功能，不向参数控件发送 SET_CUR。
        KsNode p=new KsNode { Set=xu, Id=5, Flags=0x10000002, NodeId=node };
        IntPtr data=Marshal.AllocCoTaskMem(2); uint returned;
        try {
            Marshal.WriteByte(data,0,main);Marshal.WriteByte(data,1,sub);
            int hr=control.KsProperty(ref p,(uint)Marshal.SizeOf(p),data,2,out returned);
            if(hr<0) return new Dictionary<string,object> {{"stage","select_read_function"},{"hresult",Hr(hr)},{"main",main},{"sub",sub}};
        } finally { Marshal.FreeCoTaskMem(data); }
        for(int i=0;i<50;i++) {
            var error=Get(control,node,xu,6,1);
            if((string)error["hresult"]!="0x00000000" || (string)error["data_hex"]!="01") {
                if((string)error["data_hex"]!="00") return error;
                var value=Get(control,node,xu,main,size);value["sub_function"]=sub;return value;
            }
            Thread.Sleep(10);
        }
        throw new TimeoutException("Read function selection remains busy");
    }
    static Dictionary<string,object> Get(IKsControl control, uint node, Guid set, uint id, uint size) {
        KsNode p=new KsNode { Set=set, Id=id, Flags=0x10000001, NodeId=node };
        uint returned; IntPtr data=Marshal.AllocCoTaskMem((int)size);
        try {
            for(int i=0;i<size;i++) Marshal.WriteByte(data,i,0);
            int hr=control.KsProperty(ref p,(uint)Marshal.SizeOf(p),data,size,out returned);
            byte[] raw=new byte[Math.Min(size,returned)];
            if(hr>=0) Marshal.Copy(data,raw,0,raw.Length);
            return new Dictionary<string,object> { {"node_id",node},{"property_set",set.ToString()},
                {"selector",id},{"stage","xu_get_cur"},{"hresult",Hr(hr)},{"returned",returned},
                {"data_hex",hr>=0?BitConverter.ToString(raw).Replace("-",""):""} };
        } finally { Marshal.FreeCoTaskMem(data); }
    }
    [STAThread]
    static int Main(string[] args) {
        Console.OutputEncoding=new UTF8Encoding(false);
        Console.SetError(new StreamWriter(Console.OpenStandardError(),new UTF8Encoding(false)) { AutoFlush=true });
        var result=new Dictionary<string,object>();
        object dev=null, filter=null, graph=null; IEnumMoniker enumerator=null;
        Mutex owner=null;bool owned=false;
        string stage="arguments";
        try {
            if(args.Length<2 || args.Length>4) throw new ArgumentException("Expected exact PnP interface instance, descriptor XU GUID, optional stream duration and host format height");
            string instance=args[0]; Guid xu=new Guid(args[1]);
            if(!instance.StartsWith("USB\\VID_2BDF&PID_0102&MI_",StringComparison.OrdinalIgnoreCase)) throw new ArgumentException("Unsupported target identity");
            result["target_instance"]=instance;result["backend"]="windows_usbvideo_ikscontrol";
            result["raw_record_format"]="uint32_le_length_then_payload_v1";
            result["transport_clock_is_source_sequence"]=false;
            stage="single_flight";
            string lockName="Local\\AFP_UVC_"+instance.ToUpperInvariant().Replace('\\','_').Replace('&','_');
            owner=new Mutex(false,lockName);
            try { owned=owner.WaitOne(0); } catch(AbandonedMutexException) { owned=true; }
            if(!owned)throw new InvalidOperationException("port_busy: target UVC has an active diagnostic process");
            stage="directshow_enumerate";
            dev=Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("62BE5D10-60EB-11D0-BD3B-00A0C911CE86")));
            Guid category=new Guid("860BB310-5D01-11D0-BD3B-00A0C911CE86");
            Check(((ICreateDevEnum)dev).CreateClassEnumerator(ref category,out enumerator,0));
            if(enumerator==null) throw new InvalidOperationException("No video input devices");
            var monikers=new IMoniker[1]; var matches=new List<string>();
            while(enumerator.Next(1,monikers,IntPtr.Zero)==0) {
                object bag=null; var moniker=monikers[0];
                try {
                    Guid bagId=typeof(IPropertyBag).GUID; moniker.BindToStorage(null,null,ref bagId,out bag);
                    object value; if(((IPropertyBag)bag).Read("DevicePath",out value,IntPtr.Zero)<0) continue;
                    string path=Convert.ToString(value);
                    string[] pieces=path.Split('#');
                    if(pieces.Length<3) continue;
                    string normalized="USB\\"+pieces[1]+"\\"+pieces[2];
                    if(!String.Equals(normalized,instance,StringComparison.OrdinalIgnoreCase)) continue;
                    matches.Add(path);
                    if(filter==null) {
                        Guid filterId=new Guid("56A86895-0AD4-11CE-B03A-0020AF0BA770");
                        moniker.BindToObject(null,null,ref filterId,out filter);
                    }
                } finally { Release(bag);Release(moniker); }
            }
            result["matching_device_paths"]=matches;
            if(matches.Count!=1) throw new InvalidOperationException("Target identity missing or ambiguous");
            stage="graph_add_filter";
            graph=Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("E436EBB3-524F-11CE-9F53-0020AF0BA770")));
            Check(((IGraphBuilder)graph).AddFilter((IBaseFilter)filter,"AFP target UVC"));
            stage="ks_topology"; var topology=(IKsTopologyInfo)filter;
            uint count; Check(topology.NumNodes(out count));
            var nodes=new List<object>();var reads=new List<object>();
            result["nodes"]=nodes;result["reads"]=reads;
            for(uint i=0;i<count;i++) {
                Guid type;Check(topology.NodeType(i,out type));
                nodes.Add(new { node_id=i,type=type.ToString() });
                object node=null;
                try {
                    Guid iid=typeof(IKsControl).GUID;
                    int hr=topology.CreateNodeInstance(i,ref iid,out node);
                    if(hr<0) { reads.Add(new {node_id=i,stage="create_node_ikscontrol",hresult=Hr(hr)});continue; }
                    var version=Get((IKsControl)node,i,xu,4,4);reads.Add(version);
                    reads.Add(Get((IKsControl)node,i,xu,6,1));
                    if((string)version["data_hex"]=="322E3000") {
                        reads.Add(SelectRead((IKsControl)node,i,xu,1,1,1600));
                        reads.Add(SelectRead((IKsControl)node,i,xu,3,5,1600));
                    }
                } finally { Release(node); }
            }
            if(args.Length>=3) {
                int seconds=int.Parse(args[2]);if(seconds<1||seconds>3600)throw new ArgumentException("Invalid duration");
                bool temperature=false;
                foreach(var read in reads) {
                    var d=read as Dictionary<string,object>;
                    if(d!=null&&d.ContainsKey("sub_function")&&Convert.ToUInt32(d["selector"])==3&&Convert.ToUInt32(d["sub_function"])==5&&(string)d["data_hex"]=="0108")temperature=true;
                }
                if(!temperature)throw new InvalidOperationException("Current stream is not type 8; configuration change requires operator authorization");
                int hostHeight=args.Length==4?int.Parse(args[3]):400;
                stage="directshow_raw_stream";Stream(graph,filter,result,seconds,hostHeight,xu);
            }
            result["temperature_ready"]=false;
            if(args.Length>=3&&Convert.ToInt32(result["raw_samples"])==0) {
                result["status"]="no_raw_samples";result["stage"]="directshow_raw_stream";return 3;
            }
            result["status"]=args.Length>=3?"raw_transport_only":"probed";return 0;
        } catch(Exception ex) {
            result["status"]="failed";result["stage"]=stage;result["hresult"]=Hr(ex.HResult);result["error"]=ex.Message;return 2;
        } finally {
            Release(graph);Release(filter);Release(enumerator);Release(dev);
            if(owned)owner.ReleaseMutex();if(owner!=null)owner.Dispose();
            if(args.Length>=3)Console.Error.WriteLine(new JavaScriptSerializer().Serialize(result));
            else Console.WriteLine(new JavaScriptSerializer().Serialize(result));
        }
    }
}
