pub struct Payment { pub count: u32 }
fn main() {
    let v = Payment { count: 1 };
    println!("{}", v.count);
}
