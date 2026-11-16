// Production callbacks/radial code against a typed, mutable SDK boundary.
// No game, controller emulator, Python interpreter or raw engine memory.
#include <algorithm>
#include <array>
#include <cassert>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <memory>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <vector>
#include "../../native/input_state.hpp"

std::uint64_t clock_reads=0, string_conversions=0, receiver_calls=0;
bool controlled_clock=true;
double event_time=10.;
double monotonic_seconds() noexcept {
    ++clock_reads;
    return controlled_clock ? event_time : std::chrono::duration<double>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}
int PyCallable_Check(void* p) { return p!=nullptr; }
int Py_IsInitialized() { return 1; }
int PyGILState_Check() { return 1; }
namespace py {
struct object {
    bool empty=true;
    bool is_none() const { return empty; }
    void* ptr() const { return empty ? nullptr : const_cast<object*>(this); }
    template<class... T> void operator()(T&&...) const { ++receiver_calls; }
};
object none() { return {}; }
struct gil_scoped_acquire {};
struct dict {
    struct cell { template<class T> void operator=(const T&) {} };
    cell operator[](const char*) { return {}; }
};
}
namespace unrealsdk::unreal {
std::vector<std::wstring> names;
struct FName {
    std::uint32_t index=0, number=0;
    FName(const wchar_t* name) {
        auto it=std::find(names.begin(),names.end(),name);
        if(it==names.end()) { index=static_cast<std::uint32_t>(names.size()); names.emplace_back(name); }
        else index=static_cast<std::uint32_t>(it-names.begin());
    }
    bool operator==(const FName& other) const { return index==other.index&&number==other.number; }
    bool operator!=(const FName& other) const { return !(*this==other); }
    operator std::string() const {
        ++string_conversions;
        const auto& text=names.at(index);
        return {text.begin(),text.end()};
    }
};
struct UStruct;
struct Property {
    FName name; int slot; const UStruct* owner=nullptr;
    Property(const wchar_t* n,int s):name(n),slot(s){}
    virtual ~Property()=default;
};
struct ZNameProperty:Property { using Property::Property; };
struct ZFloatProperty:Property { using Property::Property; };
struct ZBoolProperty:Property { using Property::Property; };
struct ZByteProperty:Property { using Property::Property; };
struct UStruct {
    mutable std::uint64_t lookups=0;
    std::vector<std::unique_ptr<Property>> properties;
    template<class T> void add(const wchar_t* name,int slot) {
        auto p=std::make_unique<T>(name,slot); p->owner=this; properties.push_back(std::move(p));
    }
    template<class T> const T* find_prop_and_validate(const FName& name) const {
        ++lookups;
        for(const auto& p:properties) if(p->name==name) {
            auto* typed=dynamic_cast<const T*>(p.get());
            if(!typed) throw std::runtime_error("argument/property type mismatch");
            return typed;
        }
        throw std::runtime_error("missing reflected property");
    }
};
struct UObject {
    UStruct* klass=nullptr;
    std::array<float,9> values{};
    bool using_gamepad=true;
    int class_reads=0, reads=0, writes=0, fail_read=-1, fail_write=-1;
    UStruct* Class() { ++class_reads; return klass; }
    template<class T> auto get(const T* p) {
        if(!p||p->owner!=klass) throw std::runtime_error("stale accumulator property");
        if constexpr(std::is_same_v<T,ZFloatProperty>) {
            if(reads++==fail_read) throw std::runtime_error("injected read failure");
            return values.at(p->slot);
        } else return using_gamepad;
    }
    template<class T> void set(const T* p,float value) {
        if(!p||p->owner!=klass) throw std::runtime_error("stale accumulator property");
        if(writes++==fail_write) throw std::runtime_error("injected write failure");
        values.at(p->slot)=value;
    }
};
struct WrappedStruct {
    const UStruct* type=nullptr;
    FName key{L"XboxTypeS_LeftX"}; float delta=.5f; bool gamepad=true; std::uint8_t event=0;
    template<class T> auto get(const FName& name) const {
        return get<T>(type->find_prop_and_validate<T>(name));
    }
    template<class T> auto get(const T* p) const {
        if(!p||p->owner!=type) throw std::runtime_error("stale event property");
        if constexpr(std::is_same_v<T,ZNameProperty>) return key;
        else if constexpr(std::is_same_v<T,ZFloatProperty>) return delta;
        else if constexpr(std::is_same_v<T,ZBoolProperty>) return gamepad;
        else return event;
    }
};
}
namespace unrealsdk::hook_manager {
enum class Type { PRE };
struct Details { unrealsdk::unreal::UObject* obj=nullptr; unrealsdk::unreal::WrappedStruct* args=nullptr; };
using Callback = bool(*)(Details&);
std::map<std::wstring,Callback> hooks;
int add_fault=0, remove_fault=0, has_count=0, fail_has_at=-1;
int foreign_removed=0;
bool occupy_next=false;
bool foreign_callback(Details&) { return false; }
bool has_hook(const wchar_t*,Type,const wchar_t* id) {
 if(++has_count==fail_has_at)throw std::runtime_error("has fault");
 if(occupy_next){occupy_next=false;hooks[id]=foreign_callback;return true;}
 return hooks.count(id)>0;
}
bool add_hook(const wchar_t*,Type,const wchar_t* id,Callback callback) {
 if(add_fault==3)throw std::runtime_error("before add");
 if(add_fault==5){hooks[id]=foreign_callback;return false;}
 if(add_fault!=2)hooks[id]=callback;
 if(add_fault==4)throw std::runtime_error("after add");
 return add_fault==0;
}
bool remove_hook(const wchar_t*,Type,const wchar_t* id) {
 if(remove_fault==1)throw std::runtime_error("before remove");
 if(remove_fault==3)return false;
 auto it=hooks.find(id);bool found=it!=hooks.end();
 if(found){if(it->second==foreign_callback)++foreign_removed;hooks.erase(it);}
 if(remove_fault==4)hooks[id]=foreign_callback;
 if(remove_fault==2)throw std::runtime_error("after remove");
 return found;
}
}
namespace u=unrealsdk::unreal;
namespace hm=unrealsdk::hook_manager;
namespace before {
constexpr int FIELDS=9;
#include "baseline_170/radial.inc"
Backend backend{};
#include "baseline_170/deadzone_native_axis.inc"
#include "baseline_170/deadzone_native_key.inc"
}
// Deterministic test namespace, production supplies a cryptographic nonce.
std::wstring next_hook_id(const wchar_t* label) {
 static unsigned long long sequence = 0;
 return std::wstring(label) + L"/test-nonce/" + std::to_wstring(++sequence);
}
namespace after {
constexpr int FIELDS=9;
#include "../../native/deadzone_native_event_cache.inc"
#include "../../native/deadzone_native_radial.inc"
Backend backend{};
#include "../../native/deadzone_native_axis.inc"
#include "../../native/deadzone_native_key.inc"
}
void make_axis_type(u::UStruct& type,bool reordered=false) {
    type.add<u::ZFloatProperty>(L"Viewport",0);
    type.add<u::ZFloatProperty>(L"ControllerId",0);
    if(reordered) type.add<u::ZBoolProperty>(L"bGamepad",0);
    type.add<u::ZNameProperty>(L"Key",0);
    type.add<u::ZFloatProperty>(L"Delta",0);
    type.add<u::ZFloatProperty>(L"DeltaTime",0);
    if(!reordered) type.add<u::ZBoolProperty>(L"bGamepad",0);
}
void make_key_type(u::UStruct& type) {
    type.add<u::ZFloatProperty>(L"Viewport",0);
    type.add<u::ZFloatProperty>(L"ControllerId",0);
    type.add<u::ZNameProperty>(L"Key",0);
    type.add<u::ZByteProperty>(L"EventType",0);
    type.add<u::ZFloatProperty>(L"AmountDepressed",0);
    type.add<u::ZBoolProperty>(L"bGamepad",0);
}
void make_pi_type(u::UStruct& type) {
    const wchar_t* fields[]={L"aStrafe",L"aForward",L"aBaseY",L"RawJoyRight",L"RawJoyUp",
        L"aTurn",L"aLookUp",L"RawJoyLookRight",L"RawJoyLookUp"};
    for(int i=0;i<9;++i) type.add<u::ZFloatProperty>(fields[i],i);
}
void setup() {
    before::backend.reset(); after::backend.reset();
    before::backend.input.configure(true,true,true,true,.05,.1,.1);
    after::backend.input.configure(true,true,true,true,.05,.1,.1);
    before::axis_hook::enabled=after::axis_hook::enabled=true;
    before::key_hook::enabled=after::key_hook::enabled=true;
    before::axis_hook::signal=after::axis_hook::signal=py::object{false};
    before::key_hook::notification=after::key_hook::notification=py::object{false};
    before::axis_hook::have_guard=after::axis_hook::have_guard=false;
    before::key_hook::have_guard=after::key_hook::have_guard=false;
}
void same_input() {
    const auto& a=before::backend.input; const auto& b=after::backend.input;
    assert(a.owner==b.owner&&a.raw==b.raw&&a.seen==b.seen);
    assert(a.held_keys==b.held_keys&&a.wake_active==b.wake_active&&a.last_kbm==b.last_kbm);
}
void checks() {
    setup(); u::UStruct axis_type,other_axis,key_type; make_axis_type(axis_type);
    make_axis_type(other_axis,true); make_key_type(key_type);
    u::WrappedStruct axis{&axis_type},key{&key_type}; hm::Details a{nullptr,&axis},k{nullptr,&key};
    clock_reads=0; after::axis_hook::callback(a);
    assert(clock_reads==1&&axis_type.lookups==3);
    after::axis_hook::callback(a); assert(clock_reads==2&&axis_type.lookups==3);
    axis.type=&other_axis; after::axis_hook::callback(a); assert(other_axis.lookups==3);
    after::backend.reset(); assert(!after::event_decode::axis.owner&&!after::event_decode::key.owner);
    after::axis_hook::callback(a); assert(other_axis.lookups==6);
    // Hook restarts invalidate even when the type pointer is unchanged.
    after::axis_hook::installed=true; after::axis_hook::start(py::object{false});
    after::axis_hook::callback(a); assert(other_axis.lookups==9);
    after::axis_hook::stop(); assert(!after::event_decode::axis.owner);
    after::key_hook::installed=true; after::key_hook::start(py::object{false});
    clock_reads=0; string_conversions=0; key.gamepad=true; key.key=u::FName{L"XboxTypeS_A"};
    after::key_hook::callback(k); after::key_hook::callback(k);
    assert(clock_reads==2&&key_type.lookups==3&&string_conversions==0);
    u::UStruct other_key; make_key_type(other_key); key.type=&other_key;
    after::key_hook::callback(k); assert(other_key.lookups==3&&string_conversions==0);
    after::key_hook::start(py::object{false}); after::key_hook::callback(k);
    assert(other_key.lookups==6);
    after::key_hook::stop(); assert(!after::event_decode::key.owner);
    // Failed metadata resolution publishes no partially initialized cache.
    u::UStruct wrong; wrong.add<u::ZNameProperty>(L"Key",0);
    wrong.add<u::ZBoolProperty>(L"Delta",0); wrong.add<u::ZBoolProperty>(L"bGamepad",0);
    after::axis_hook::enabled=true; axis.type=&wrong;
    after::axis_hook::callback(a); assert(!after::axis_hook::enabled&&!after::event_decode::axis.owner);
    wrong.properties.clear(); wrong.add<u::ZNameProperty>(L"Key",0);
    wrong.add<u::ZByteProperty>(L"EventType",0); // missing gamepad property
    after::key_hook::enabled=true; key.type=&wrong;
    after::key_hook::callback(k); assert(!after::key_hook::enabled&&!after::event_decode::key.owner);
    // Old/new event sequences are identical at controlled timing boundaries.
    setup(); axis.type=&axis_type; key.type=&key_type;
    auto axis_event=[&](const wchar_t* name,float delta,bool gamepad,double time) {
        axis.key=u::FName{name}; axis.delta=delta; axis.gamepad=gamepad; event_time=time;
        before::axis_hook::callback(a); after::axis_hook::callback(a); same_input();
    };
    auto key_event=[&](const wchar_t* name,int event,bool gamepad,double time) {
        key.key=u::FName{name}; key.event=static_cast<std::uint8_t>(event); key.gamepad=gamepad; event_time=time;
        before::key_hook::callback(k); after::key_hook::callback(k); same_input();
    };
    key_event(L"W",0,false,10.); axis_event(L"XboxTypeS_LeftX",.5f,true,10.099);
    key_event(L"W",2,false,10.1); key_event(L"W",1,false,10.11);
    axis_event(L"XboxTypeS_LeftX",.5f,true,10.199);
    axis_event(L"XboxTypeS_LeftX",.5f,true,10.201);
    key_event(L"LeftMouseButton",0,false,10.3); key_event(L"LeftMouseButton",1,false,10.31);
    key_event(L"XboxTypeS_A",0,true,10.399); key_event(L"XboxTypeS_A",0,true,10.401);
    axis_event(L"XboxTypeS_LeftX",0,true,10.41);
    axis_event(L"XboxTypeS_RightY",.8f,true,10.42);
    axis_event(L"MouseX",.1f,false,10.619); axis_event(L"MouseX",.1f,false,10.621);
    assert(before::backend.input.frame_allowed(true,10.656)==after::backend.input.frame_allowed(true,10.656));
    assert(before::backend.input.guard(10.7)==after::backend.input.guard(10.7)); same_input();
    // Class cache invalidation, disabled-stick values, curve parity and atomic reads.
    u::UStruct pi_type,other_pi; make_pi_type(pi_type); make_pi_type(other_pi);
    u::UObject old_pi{&pi_type},new_pi{&pi_type};
    const auto old_addr=reinterpret_cast<uintptr_t>(&old_pi),new_addr=reinterpret_cast<uintptr_t>(&new_pi);
    const double nan=std::numeric_limits<double>::quiet_NaN();
    for(auto inner:{0.,.1,.9,1.}) for(auto outer:{0.,.5,1.}) for(int mask=1;mask<=3;++mask) {
        const bool left=mask&1,right=mask&2;
        old_pi.values.fill(.8f); new_pi.values=old_pi.values; new_pi.class_reads=0;
        const int x=before::backend.apply(old_addr,left,right,left?.3:nan,left?-.4:nan,
            right?.5:nan,right?-.6:nan,left?inner:nan,left?outer:nan,right?inner:nan,right?outer:nan);
        const int y=after::backend.apply(new_addr,left,right,left?.3:nan,left?-.4:nan,
            right?.5:nan,right?-.6:nan,left?inner:nan,left?outer:nan,right?inner:nan,right?outer:nan);
        assert(x==1&&y==1&&old_pi.values==new_pi.values&&new_pi.class_reads==1);
    }
    new_pi.klass=&other_pi; new_pi.values.fill(1.);
    assert(after::backend.apply(new_addr,false,true,0,0,.5,0,.1,1,.1,1)==1);
    assert(other_pi.lookups==4);
    after::backend.reset(); new_pi.reads=new_pi.writes=0; new_pi.fail_read=2;
    const auto original=new_pi.values;
    assert(after::backend.apply(new_addr,true,true,.5,0,.5,0,.1,1,.1,1)==0);
    assert(new_pi.values==original&&new_pi.writes==0&&!after::backend.quarantined);
    after::backend.reset(); new_pi.fail_read=-1; new_pi.reads=new_pi.writes=0; new_pi.fail_write=1;
    new_pi.values.fill(1.);
    assert(after::backend.apply(new_addr,true,true,.5,0,.5,0,.1,1,.1,1)==-1);
    assert(after::backend.quarantined);
    const int writes=new_pi.writes;
    assert(after::backend.apply(new_addr,true,true,.5,0,.5,0,.1,1,.1,1)==0&&new_pi.writes==writes);
}
volatile double benchmark_sink=0.;
template<class F> double measure(F&& run) {
    const auto start=std::chrono::steady_clock::now();
    run(); return std::chrono::duration<double,std::nano>(std::chrono::steady_clock::now()-start).count();
}
template<class A,class B> void comparison(const char* name,int count,A&& old_run,B&& new_run,bool comma) {
    old_run(1000); new_run(1000);
    std::vector<double> old_ns,new_ns;
    for(int round=0;round<7;++round) {
        if(round%2) { new_ns.push_back(measure([&]{new_run(count);})/count); old_ns.push_back(measure([&]{old_run(count);})/count); }
        else { old_ns.push_back(measure([&]{old_run(count);})/count); new_ns.push_back(measure([&]{new_run(count);})/count); }
    }
    std::sort(old_ns.begin(),old_ns.end()); std::sort(new_ns.begin(),new_ns.end());
    std::cout<<(comma?",\n":"")<<"    {\"scenario\":\""<<name<<"\",\"events_per_round\":"<<count
        <<",\"rounds\":7,\"before_median_ns\":"<<old_ns[3]<<",\"after_median_ns\":"<<new_ns[3]
        <<",\"reduction_percent\":"<<(1.-new_ns[3]/old_ns[3])*100.<<"}";
}
void benchmark() {
    setup(); controlled_clock=false;
    u::UStruct axis_type,key_type,pi_type; make_axis_type(axis_type); make_key_type(key_type); make_pi_type(pi_type);
    u::WrappedStruct axis{&axis_type},key{&key_type}; hm::Details a{nullptr,&axis},k{nullptr,&key};
    const wchar_t* axes[]={L"XboxTypeS_LeftX",L"XboxTypeS_LeftY",L"XboxTypeS_RightX",L"XboxTypeS_RightY",L"MouseX",L"MouseY"};
    std::array<u::FName,6> trace={u::FName{axes[0]},u::FName{axes[1]},u::FName{axes[2]},u::FName{axes[3]},u::FName{axes[4]},u::FName{axes[5]}};
    auto old_axis=[&](int count) { for(int i=0;i<count;++i) { axis.key=trace[i%6]; axis.gamepad=i%6<4; axis.delta=i%8?.4f:0.f; before::axis_hook::callback(a); } benchmark_sink=before::backend.input.raw[0]; };
    auto new_axis=[&](int count) { for(int i=0;i<count;++i) { axis.key=trace[i%6]; axis.gamepad=i%6<4; axis.delta=i%8?.4f:0.f; after::axis_hook::callback(a); } benchmark_sink=after::backend.input.raw[0]; };
    key.key=u::FName{L"XboxTypeS_RightShoulder"}; key.gamepad=true;
    auto old_key=[&](int count) { for(int i=0;i<count;++i) { key.event=static_cast<std::uint8_t>(i%3); before::key_hook::callback(k); } benchmark_sink=static_cast<int>(before::backend.input.owner); };
    auto new_key=[&](int count) { for(int i=0;i<count;++i) { key.event=static_cast<std::uint8_t>(i%3); after::key_hook::callback(k); } benchmark_sink=static_cast<int>(after::backend.input.owner); };
    u::UObject old_pi{&pi_type},new_pi{&pi_type};
    auto old_radial=[&](int count) { for(int i=0;i<count;++i) { old_pi.values.fill(.8f); before::backend.apply(reinterpret_cast<uintptr_t>(&old_pi),false,true,0,0,.4,.5,.1,1,.1,1); } benchmark_sink=old_pi.values[5]; };
    auto new_radial=[&](int count) { for(int i=0;i<count;++i) { new_pi.values.fill(.8f); after::backend.apply(reinterpret_cast<uintptr_t>(&new_pi),false,true,0,0,.4,.5,.1,1,.1,1); } benchmark_sink=new_pi.values[5]; };
    std::cout<<std::fixed<<std::setprecision(3)<<"{\n  \"status\":\"passed\",\n  \"baseline_commit\":\"af03a3f6f054b4370a8bd86c9efd7d20fa14dd72\",\n"
        <<"  \"method\":\"Win32 Release; production before/after callbacks with a typed SDK boundary, 1000 warmup events and seven alternating paired rounds\",\n"
        <<"  \"limitations\":\"Synthetic argument storage, FName conversion and notification boundary; excludes SDK dispatch, real GIL and game rendering. Callback CPU cost only, not FPS.\",\n  \"scenarios\":[\n";
    comparison("axis_mixed_input",100000,old_axis,new_axis,false);
    comparison("gamepad_key",100000,old_key,new_key,true);
    comparison("right_only_radial",100000,old_radial,new_radial,true);
    std::cout<<"\n  ]\n}\n";
}
int main(int argc,char** argv) {
    static_assert(sizeof(void*)==4,"Run the release's Win32 architecture");
    try {
        if(argc==2&&std::string{argv[1]}=="--benchmark") benchmark();
        else { checks(); std::cout<<"PASS: production event caches, timing parity, supplied key behavior, radial enabled-stick/class/failure checks\n"; }
    } catch(const std::exception& e) { std::cerr<<e.what()<<"\n"; return 1; }
}
