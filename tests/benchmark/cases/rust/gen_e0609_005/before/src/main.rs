pub struct Invoice { pub size: u32 }
fn main() {
    let v = Invoice { size: 1 };
    println!("{}", v.length);
}
