// Exercises production lifecycle observers through the existing native test boundary.
#define main included_native_main
#include "test_native.cpp"
#undef main
#include <random>
#include <set>
namespace hm=unrealsdk::hook_manager;
void clear_faults(){hm::add_fault=0;hm::remove_fault=0;hm::fail_remove=false;hm::fail_has_at=-1;}
void reset_lifecycle(){clear_faults();lifecycle::stop();assert(hm::hooks.empty());}
int main(){
 static_assert(sizeof(void*)==4,"Win32 required");
 constexpr uintptr_t pi=1234;std::set<std::wstring> ids;
 for(int i=0;i<1000;++i){
  hm::add_fault=i%2;assert(lifecycle::start(pi));assert(hm::hooks.size()==3);
  for(const auto& id:lifecycle::IDs){assert(ids.insert(id).second);assert(hm::hooks.at(id)==lifecycle::callback);}
  assert(lifecycle::start(pi));assert(hm::hooks.size()==3);
  lifecycle::stop();assert(hm::hooks.empty());
 }
 for(int mode : {2,3,4}){
  reset_lifecycle();hm::add_fault=mode;
  try{assert(!lifecycle::start(pi));}catch(const std::runtime_error&){}
  clear_faults();lifecycle::stop();assert(hm::hooks.empty());
 }
 for(int mode : {1,2,3}){
  reset_lifecycle();assert(lifecycle::start(pi));hm::remove_fault=mode;lifecycle::stop();
  assert(!lifecycle::start(pi));clear_faults();lifecycle::stop();assert(hm::hooks.empty());
  assert(lifecycle::start(pi));lifecycle::stop();assert(hm::hooks.empty());
 }
 reset_lifecycle();assert(lifecycle::start(pi));auto first=lifecycle::IDs[0];
 hm::remove_fault=4;hm::fail_has_at=hm::has_count+1;lifecycle::stop();
 assert(hm::has_count+1==hm::fail_has_at); // true removals skip verification
 assert(hm::hooks.at(first)==hm::foreign_callback);
 lifecycle::stop();assert(hm::hooks.at(first)==hm::foreign_callback);
 hm::hooks.clear();clear_faults();
 for(unsigned seed : {7u,20261114u,846203u}){
  reset_lifecycle();std::mt19937 rng(seed);hm::foreign_removed=0;
  hm::hooks[L"unrelated-foreign"]=hm::foreign_callback;
  std::vector<unsigned> trace;
  for(int i=0;i<5000;++i){
   auto op=rng()%2;unsigned fault;
   if(op){fault=rng()%5;hm::add_fault=static_cast<int>(fault);try{lifecycle::start(pi);}catch(const std::runtime_error&){} }
   else {fault=rng()%4;hm::remove_fault=static_cast<int>(fault);lifecycle::stop();}
   trace.push_back(op*10+fault);if(trace.size()>30)trace.erase(trace.begin());
   if(hm::hooks.count(L"unrelated-foreign")!=1 || hm::hooks.at(L"unrelated-foreign")!=hm::foreign_callback ||
      hm::foreign_removed!=0 || hm::hooks.size()>4){
    std::cerr<<"seed="<<seed<<" step="<<i<<" recent(op*10+fault)=";
    for(auto event:trace)std::cerr<<event<<",";std::cerr<<"\n";std::abort();
   }
  }
  clear_faults();lifecycle::stop();assert(hm::hooks.size()==1);hm::hooks.clear();
 }
 std::cout<<"PASS: production lifecycle observers; 3000 IDs; 15000 seeded steps\n";
}
