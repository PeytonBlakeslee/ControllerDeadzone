// Executes the production field, binding-discovery and lifecycle implementations
// against a small reflected-property boundary. Compile Win32: guards stay real.
#include <algorithm>
#include <array>
#include <cassert>
#include <cmath>
#include <cstdint>
#include <iostream>
#include <map>
#include <limits>
#include <stdexcept>
#include <string>
#include <tuple>
#include <type_traits>
#include <vector>
#include "../../native/input_state.hpp"

namespace py {
struct item {
 double number=0;size_t count=0;
 template<class T>T cast()const{return static_cast<T>(number);}
};
struct list {size_t count=0;template<class T>void append(T){++count;}};
struct tuple {std::vector<item> values;item operator[](size_t n)const{return values.at(n);}};
inline item value(const list& l){return {0,l.count};}
inline item value(const std::string&){return {};}
template<class T>item value(T x){return {static_cast<double>(x),0};}
template<class...T>tuple make_tuple(T...x){return {{value(x)...}};}
inline size_t len(item i){return i.count;}
struct dict {
 struct cell {template<class T>void operator=(const T&) {}};
 cell operator[](const char*){return {};}
};
}
namespace unrealsdk::unreal {
struct FName {
 std::wstring text;
 FName(const wchar_t* s):text(s){}
 bool operator==(const FName& o)const{return text==o.text;}
 bool operator!=(const FName& o)const{return !(*this==o);}
 operator std::string()const{return {text.begin(),text.end()};}
};
struct ZArrayProperty{};struct ZStructProperty{};struct ZFloatProperty{};
struct ZNameProperty{};struct ZObjectProperty{};struct ZIntProperty{};
struct UObject;
struct Row {FName name{L""};float speed=1,dz=.2f;UObject* device=nullptr;int index=0;};
struct ClassInfo {FName name{L"InputDeviceDefinition"};FName Name()const{return name;}};
struct WrappedStruct {
 UObject* object;size_t index;bool handler=false;
 template<class P>auto get(FName name)const;
 template<class P>void set(FName name,float value);
};
struct WrappedArray {
 UObject* object;
 size_t size()const;
 template<class P>WrappedStruct get_at(size_t n)const;
};
struct UObject {
 ClassInfo klass;FName name{L"InputDevice_Controller"};std::vector<Row> rows;
 int read_errors=0,nan_reads=0,writes=0;bool fail_readback=false,fail_before_write=false,nonfinite_readback=false;
 ClassInfo* Class(){return &klass;}FName Name()const{return name;}
 template<class P>WrappedArray get(FName){return {this};}
};
size_t WrappedArray::size()const{return object->rows.size();}
template<class P>WrappedStruct WrappedArray::get_at(size_t n)const {
 if(n>=size())throw std::runtime_error("array index out of range");
 return {object,n,false};
}
template<class P>auto WrappedStruct::get(FName name)const {
 auto& r=object->rows.at(index);
 if constexpr(std::is_same_v<P,ZFloatProperty>){
   if(object->nan_reads>0){--object->nan_reads;return std::numeric_limits<float>::quiet_NaN();}
   if(object->read_errors>0){--object->read_errors;throw std::runtime_error("readback unavailable");}
   return name==FName{L"Speed"}?r.speed:r.dz;
 }else if constexpr(std::is_same_v<P,ZNameProperty>)return r.name;
 else if constexpr(std::is_same_v<P,ZStructProperty>)return WrappedStruct{object,index,true};
 else if constexpr(std::is_same_v<P,ZObjectProperty>)return r.device;
 else if constexpr(std::is_same_v<P,ZIntProperty>)return r.index;
}
template<class P>void WrappedStruct::set(FName name,float v){
 if(object->fail_before_write){object->fail_before_write=false;throw std::runtime_error("set unavailable");}
 auto& r=object->rows.at(index);++object->writes;
 (name==FName{L"Speed"}?r.speed:r.dz)=v;
 if(object->fail_readback){object->fail_readback=false;object->read_errors=1;}
 if(object->nonfinite_readback){object->nonfinite_readback=false;object->nan_reads=1;}
}
}
namespace unrealsdk::hook_manager {
enum class Type{PRE};
struct Details{unrealsdk::unreal::UObject* obj=nullptr;};
using Callback=bool(*)(Details&);
std::map<std::wstring,Callback> hooks;
bool fail_remove=false;std::wstring fail_path;
int add_fault=0,remove_fault=0,foreign_removed=0,has_count=0,fail_has_at=-1;
bool foreign_callback(Details&){return false;}
bool has_hook(const wchar_t*,Type,const wchar_t* id){
 if(++has_count==fail_has_at)throw std::runtime_error("has fault");
 return hooks.count(id)>0;
}
bool add_hook(const wchar_t* path,Type,const wchar_t* id,Callback callback){
 if(path==fail_path||add_fault==2)return false;
 if(add_fault==3)throw std::runtime_error("before add");
 if(add_fault==5){hooks[id]=foreign_callback;return false;}
 hooks[id]=callback;
 if(add_fault==4)throw std::runtime_error("after add");
 return add_fault==0;
}
bool remove_hook(const wchar_t*,Type,const wchar_t* id){
 if(fail_remove||remove_fault==1)throw std::runtime_error("hook removal failed");
 if(remove_fault==3)return false;
 auto it=hooks.find(id);bool found=it!=hooks.end();
 if(found){if(it->second==foreign_callback)++foreign_removed;hooks.erase(it);}
 if(remove_fault==4)hooks[id]=foreign_callback;
 if(remove_fault==2)throw std::runtime_error("after remove");
 return found;
}
}
struct Backend {int resets=0;void reset(){++resets;}}backend;
namespace preprocess_hook {
bool installed=false,enabled=false;uintptr_t owner=0;
void suspend(){enabled=false;owner=0;}
bool prepared(){return installed&&owner;}
bool active(){return prepared()&&enabled;}
bool activate(uintptr_t pi){if(!prepared()||owner!=pi)return false;enabled=true;return true;}
void prepare(uintptr_t pi){installed=true;owner=pi;enabled=false;}
}
// Deterministic test namespace, production supplies a cryptographic nonce.
std::wstring next_hook_id(const wchar_t* label) {
 static unsigned long long sequence = 0;
 return std::wstring(label) + L"/test-nonce/" + std::to_wstring(++sequence);
}
#include "../../native/deadzone_native_fields.inc"
#include "../../native/deadzone_native_discovery.inc"
#include "../../native/deadzone_native_lifecycle.inc"

int main(){
 static_assert(sizeof(void*)==4,"Execute with the release's Win32 architecture");
 using namespace unrealsdk::unreal;
 UObject device;
 for(auto name:axis_fields::PHYSICAL)device.rows.push_back(Row{FName{name}});
 const auto addr=reinterpret_cast<uintptr_t>(&device);
 assert(axis_fields::cas(addr,0,0,0,1,0)==1);
 assert(device.rows[0].speed==0&&axis_fields::owned.size()==1);
 assert(axis_fields::restore_owned(addr,0,0,0,1)==1);
 assert(device.rows[0].speed==1&&axis_fields::owned.empty());
 device.nonfinite_readback=true;
 assert(axis_fields::cas(addr,0,0,0,1,0)==-1);
 assert(axis_fields::owned.size()==1);
 assert(axis_fields::restore_owned(addr,0,0,0,1)==1);
 assert(device.rows[0].speed==1&&axis_fields::owned.empty());
 assert(axis_fields::cas(addr,0,0,0,.5,0)==0);
 assert(device.rows[0].speed==1);
 assert(axis_fields::cas(addr,0,0,0,1,0)==1);
 device.rows[0].speed=.7f;
 assert(axis_fields::restore_owned(addr,0,0,0,1)==0);
 assert(device.rows[0].speed==.7f&&axis_fields::owned.empty());
 device.rows[0].speed=1;device.fail_readback=true;
 assert(axis_fields::cas(addr,0,0,0,1,0)==-1);
 assert(axis_fields::owned.size()==1);
 const int writes=device.writes;
 assert(axis_fields::cas(addr,0,0,0,0,.2)==-1);
 assert(device.writes==writes);
 assert(axis_fields::restore_owned(addr,0,0,0,1)==1);
 assert(device.rows[0].speed==1&&axis_fields::owned.empty());
 assert(axis_fields::cas(addr,0,0,0,1,0)==1);
 device.fail_before_write=true;
 assert(axis_fields::cas(addr,0,0,0,0,.2)==-1);
 assert(device.rows[0].speed==0);
 assert(axis_fields::restore_owned(addr,0,0,0,1)==1);
 assert(device.rows[0].speed==1&&axis_fields::owned.empty());
 assert(axis_fields::cas(addr,20,0,0,1,0)==-1);
 device.name=FName{L"MouseDevice"};
 assert(axis_fields::cas(addr,0,0,0,1,0)==-1);
 device.name=FName{L"InputDevice_Controller"};

 UObject pi;const auto pi_addr=reinterpret_cast<uintptr_t>(&pi);
 const wchar_t* logical[]={L"aStrafe",L"aForward",L"aTurn",L"aLookUp"};
 for(int i=0;i<4;++i)pi.rows.push_back(Row{FName{logical[i]},1,.2f,&device,i});
 assert(py::len(binding_discovery::scan(pi_addr)[1])==4);
 pi.rows.push_back(pi.rows[0]);
 assert(py::len(binding_discovery::scan(pi_addr)[1])==4);
 pi.rows.back().name=FName{L"aTurn"};
 assert(py::len(binding_discovery::scan(pi_addr)[2])==1);
 pi.rows.pop_back();

 namespace hm=unrealsdk::hook_manager;
 hm::fail_path=lifecycle::PATHS[1];
 assert(!lifecycle::start(pi_addr)); // partial observer registration is insufficient
 hm::fail_path.clear();assert(lifecycle::start(pi_addr));
 preprocess_hook::prepare(pi_addr);
 pi.rows.pop_back();
 assert(!lifecycle::resume(pi_addr));assert(!preprocess_hook::active());
 pi.rows.push_back(Row{FName{logical[3]},1,.2f,&device,3});
 preprocess_hook::prepare(pi_addr);
 assert(lifecycle::resume(pi_addr));assert(preprocess_hook::active());
 assert(!lifecycle::begin(pi_addr+4));assert(preprocess_hook::active());
 assert(lifecycle::begin(pi_addr));assert(!preprocess_hook::active());
 hm::fail_remove=true;lifecycle::stop();
 assert(hm::hooks.size()==3);assert(!lifecycle::active());
 hm::fail_remove=false;assert(!lifecycle::start(pi_addr));
 lifecycle::stop();assert(hm::hooks.empty());
 assert(lifecycle::start(pi_addr));lifecycle::stop();assert(hm::hooks.empty());

 deadzone::InputState input;input.configure(true,true,true,true,.05,.1,.1);
 input.key("W",false,0,10.);input.axis(0,.5,true,10.01);
 assert(input.owner==deadzone::InputState::KeyboardMouse);
 const auto guard=input.guard(10.01);assert(!guard.first&&guard.second);
 assert(!input.frame_allowed(true,10.01));
 input.key("W",false,1,10.02);input.axis(0,.5,true,10.2);
 assert(input.owner==deadzone::InputState::Controller);
 assert(input.frame_allowed(true,10.2));
 input.axis(0,0,true,10.21);assert(input.sample(0,10.21)==0);
 input.axis(2,.8,true,10.22);assert(input.sample(2,10.5)==0);
 input.configure(true,true,false,true,.05,1.,.1);assert(input.guard(10.5).first);
 input.reset();assert(input.owner==deadzone::InputState::Unknown&&input.held_keys.empty());
 std::cout<<"PASS: Win32 production ownership, partial writes, binding discovery, lifecycle and input arbitration\n";
}
